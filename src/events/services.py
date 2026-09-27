"""Every write to an event, its tracks, prizes, questions and organizers.

Permission rule: an event is managed by the organizers linked to it (the creator is linked
automatically) and by any admin. `can_manage` is the only definition of that rule.
"""

from django.db import transaction
from django.http import Http404

from accounts.models import User
from accounts.roles import Role
from core import audit
from core.models import AuditAction
from events.models import Event, EventOrganizer


class EventRuleError(Exception):
    """A refused change, with a sentence the UI can show as-is."""


def can_manage(user, event):
    if not user.is_authenticated:
        return False
    if user.role == Role.ADMIN:
        return True
    return user.role == Role.ORGANIZER and event.organizer_links.filter(user=user).exists()


def get_managed_event(user, slug):
    """The event, if `user` may manage it. Otherwise 404 -- not 403 -- so an organizer cannot
    probe which slugs other organizers are using."""
    event = Event.objects.filter(slug=slug).first()
    if event is None or not can_manage(user, event):
        raise Http404("No such event.")
    return event


def get_visible_event(user, slug):
    """Published events are public; unpublished ones exist only for their managers."""
    event = Event.objects.filter(slug=slug).first()
    if event is None or not (event.is_published or can_manage(user, event)):
        raise Http404("No such event.")
    return event


def create_event(request, form):
    with transaction.atomic():
        event = form.save(commit=False)
        event.created_by = request.user
        event.save()
        EventOrganizer.objects.create(event=event, user=request.user, added_by=request.user)
    audit.record(AuditAction.EVENT_CREATED, request=request, subject=event.slug)
    return event


def update_event(request, event, form):
    changed = form.changed_data
    old_close = Event.objects.values_list("submissions_close_at", flat=True).get(pk=event.pk)
    event = form.save()
    if "submissions_close_at" in changed and event.submissions_close_at != old_close:
        audit.record(
            AuditAction.DEADLINE_CHANGED, request=request, subject=event.slug,
            old=old_close.isoformat(), new=event.submissions_close_at.isoformat(),
        )
    if changed:
        audit.record(AuditAction.EVENT_UPDATED, request=request, subject=event.slug, fields=changed)
    return event


def set_published(request, event, published):
    if event.is_published == published:
        return event
    event.is_published = published
    event.save(update_fields=["is_published", "updated_at"])
    action = AuditAction.EVENT_PUBLISHED if published else AuditAction.EVENT_UNPUBLISHED
    audit.record(action, request=request, subject=event.slug)
    return event


# --- tracks, prizes, questions ----------------------------------------------------------


def save_part(request, event, form, kind):
    """Create or edit a track, prize or question from its ModelForm."""
    created = form.instance.pk is None
    part = form.save(commit=False)
    part.event = event
    part.save()
    audit.record(
        AuditAction.EVENT_PART_CHANGED, request=request, subject=event.slug,
        kind=kind, id=part.pk, change="created" if created else "edited",
    )
    return part


def set_part_hidden(request, event, part, kind, hidden):
    part.is_hidden = hidden
    part.save(update_fields=["is_hidden"])
    audit.record(
        AuditAction.EVENT_PART_CHANGED, request=request, subject=event.slug,
        kind=kind, id=part.pk, change="hidden" if hidden else "shown",
    )


def delete_part(request, event, part, kind):
    """Delete a track, prize or question -- unless something already refers to it.

    A track with projects, or a question with answers, can only be hidden: deleting it would
    silently change what those submissions say.
    """
    if kind == "track" and part.projects.exists():
        raise EventRuleError(
            f'"{part.name}" has projects in it, so it can only be hidden, not deleted.'
        )
    if kind == "question" and part.answers.exists():
        raise EventRuleError("This question already has answers, so it can only be hidden.")
    pk = part.pk
    part.delete()
    audit.record(
        AuditAction.EVENT_PART_CHANGED, request=request, subject=event.slug,
        kind=kind, id=pk, change="deleted",
    )


def question_kind_locked(question):
    """Changing an answered question's kind would reinterpret every stored answer."""
    return question.pk is not None and question.answers.exists()


# --- co-organizers ----------------------------------------------------------------------


def add_organizer(request, event, email):
    user = User.objects.filter(email=email.strip().lower()).first()
    if user is None:
        raise EventRuleError(
            "No account has that email. An admin can create an organizer account first."
        )
    if user.role not in (Role.ORGANIZER, Role.ADMIN):
        raise EventRuleError(f"{user.email} is a {user.role}, not an organizer.")
    if user.role == Role.ADMIN:
        raise EventRuleError("Admins can already manage every event.")
    _, created = EventOrganizer.objects.get_or_create(
        event=event, user=user, defaults={"added_by": request.user}
    )
    if not created:
        raise EventRuleError(f"{user.email} already organizes this event.")
    audit.record(AuditAction.ORGANIZER_ADDED, request=request, subject=event.slug, email=user.email)


def remove_organizer(request, event, link):
    if event.organizer_links.count() <= 1:
        raise EventRuleError("An event needs at least one organizer.")
    email = link.user.email
    link.delete()
    audit.record(AuditAction.ORGANIZER_REMOVED, request=request, subject=event.slug, email=email)


# --- deadlines and extensions ---------------------------------------------------------------


def extend_deadline(request, event, new_close, reason):
    """Move the close later for everyone. Judging keeps its length: if the new close would pass
    the judging end, the judging end moves by the same amount. The first close is kept in
    `original_submissions_close_at` so pages can say what changed."""
    old_close = event.submissions_close_at
    if new_close <= old_close:
        raise EventRuleError("An extension must move the close later. To bring it earlier, edit the settings.")
    if event.original_submissions_close_at is None:
        event.original_submissions_close_at = old_close
    if new_close > event.judging_ends_at:
        event.judging_ends_at = event.judging_ends_at + (new_close - old_close)
    event.submissions_close_at = new_close
    event.save(update_fields=[
        "submissions_close_at", "original_submissions_close_at", "judging_ends_at", "updated_at",
    ])
    audit.record(
        AuditAction.DEADLINE_EXTENDED, request=request, subject=event.slug,
        old=old_close.isoformat(), new=new_close.isoformat(), reason=reason,
    )
    return event


def grant_extension(request, event, team, until, reason):
    from teams.models import TeamExtension

    if team.event_id != event.pk:
        raise EventRuleError("That team is not in this event.")
    if until <= event.submissions_close_at:
        raise EventRuleError("An extension must end after the event's close, or it changes nothing.")
    from core.deadlines import db_now

    if until <= db_now():
        raise EventRuleError("An extension must end in the future.")
    if until > event.judging_ends_at:
        raise EventRuleError("An extension cannot run past the end of judging.")
    TeamExtension.objects.update_or_create(
        team=team, defaults={"until": until, "reason": reason, "granted_by": request.user},
    )
    audit.record(
        AuditAction.TEAM_EXTENSION_GRANTED, request=request, subject=team.name,
        event=event.slug, until=until.isoformat(), reason=reason,
    )


def revoke_extension(request, event, extension):
    name = extension.team.name
    extension.delete()
    audit.record(AuditAction.TEAM_EXTENSION_REVOKED, request=request, subject=name, event=event.slug)
