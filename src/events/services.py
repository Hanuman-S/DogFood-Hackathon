"""Every write to an event, its tracks, prizes, questions, and who holds which role in it.

Permission rule: an event is managed by its organizers (the creator becomes one automatically)
and by any platform admin. `can_manage` is the only definition of that rule.
"""

import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

from accounts.models import User
from accounts.roles import Role, forget_cached_roles, is_organizer_of, roles_in
from core import audit
from core.models import AuditAction
from events.models import Event, EventMembership, JudgeInvite, JudgeTrack


class EventRuleError(Exception):
    """A refused change, with a sentence the UI can show as-is."""


def can_manage(user, event):
    return is_organizer_of(user, event)


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
        EventMembership.objects.create(
            event=event, user=request.user, role=Role.ORGANIZER, added_by=request.user
        )
    forget_cached_roles(request.user)
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


def publish_blockers(event):
    """What must be fixed before `event` can be published, as sentences. Empty = ready.

    Publishing opens the event to participants, so it must say what it is, and it must already
    have a rubric: the rubric locks when submissions close, and an event that reached its close
    without one could never be judged. Tracks and prizes stay optional (no tracks = one open
    category).
    """
    blockers = []
    if not event.tagline.strip():
        blockers.append("add a tagline.")
    if not event.description.strip():
        blockers.append("add a description.")
    criteria = list(event.criteria.all())
    if not criteria:
        blockers.append("set up the rubric (judges need it, and it locks when submissions close).")
    return blockers


def set_published(request, event, published):
    if event.is_published == published:
        return event
    if published:
        blockers = publish_blockers(event)
        if blockers:
            raise EventRuleError("not published yet: " + " ".join(blockers))
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


# --- staff: co-organizers and judges ---------------------------------------------------------


def _staff_candidate(event, email, role):
    """The account to make `role` in `event`, or a sentence saying why not."""
    user = User.objects.filter(email=email.strip().lower()).first()
    if user is None:
        raise EventRuleError(
            "No account has that email. Use 'invite by link' instead: they create their account "
            "from the link."
        )
    if user.is_platform_admin:
        raise EventRuleError("Platform admins can already manage every event.")
    held = roles_in(user, event)
    if role in held:
        raise EventRuleError(f"{user.email} is already a {role} in this event.")
    if Role.PARTICIPANT in held:
        raise EventRuleError(
            f"{user.email} is competing in this event, so they cannot also be a {role} in it "
            "(conflict of interest)."
        )
    return user


def _grant(request, event, user, role):
    try:
        with transaction.atomic():
            membership = EventMembership.objects.create(
                event=event, user=user, role=role, added_by=request.user
            )
    except IntegrityError as error:
        # The exclusion constraint (or a double-click) beat the check above.
        raise EventRuleError(
            f"{user.email} cannot be a {role} in this event (conflict of interest)."
        ) from error
    forget_cached_roles(user)
    return membership


def add_organizer(request, event, email):
    user = _staff_candidate(event, email, Role.ORGANIZER)
    _grant(request, event, user, Role.ORGANIZER)
    audit.record(AuditAction.ORGANIZER_ADDED, request=request, subject=event.slug, email=user.email)


def remove_organizer(request, event, membership):
    if event.memberships.filter(role=Role.ORGANIZER).count() <= 1:
        raise EventRuleError("An event needs at least one organizer.")
    email = membership.user.email
    membership.delete()
    audit.record(AuditAction.ORGANIZER_REMOVED, request=request, subject=event.slug, email=email)


def add_judge(request, event, email, tracks=()):
    """Make an account a judge of this event, optionally for some of its tracks."""
    user = _staff_candidate(event, email, Role.JUDGE)
    with transaction.atomic():
        membership = _grant(request, event, user, Role.JUDGE)
        for track in tracks:
            if track.event_id != event.pk:
                raise EventRuleError("That track is not in this event.")
            JudgeTrack.objects.create(membership=membership, track=track)
    audit.record(
        AuditAction.JUDGE_ADDED, request=request, subject=event.slug, email=user.email,
        tracks=[t.name for t in tracks],
    )
    return membership


# --- judge invite links ------------------------------------------------------------------------

INVITE_TTL = timedelta(days=7)
INVITE_PREFIX = "jinv_"            # judge invite links
ORGANIZER_INVITE_PREFIX = "oinv_"  # co-organizer invite links

# Audit actions per invite role: (invited, accepted, revoked, refused, added).
_INVITE_ACTIONS = {
    Role.JUDGE: (AuditAction.JUDGE_INVITED, AuditAction.JUDGE_INVITE_ACCEPTED,
                 AuditAction.JUDGE_INVITE_REVOKED, AuditAction.JUDGE_INVITE_REFUSED, AuditAction.JUDGE_ADDED),
    Role.ORGANIZER: (AuditAction.ORGANIZER_INVITED, AuditAction.ORGANIZER_INVITE_ACCEPTED,
                     AuditAction.ORGANIZER_INVITE_REVOKED, AuditAction.ORGANIZER_INVITE_REFUSED,
                     AuditAction.ORGANIZER_ADDED),
}


def invite_problem(event, user, role):
    """Why `user` cannot accept a `role` invite to `event`, or ""."""
    if role == Role.JUDGE:
        return judge_invite_problem(event, user)
    if user.is_platform_admin:
        return "Platform admins can already manage every event."
    held = roles_in(user, event)
    if Role.ORGANIZER in held:
        return f"{user.email} is already an organizer of this event."
    if Role.PARTICIPANT in held:
        return (
            f"{user.email} is competing in this event, so they cannot also organize it "
            "(conflict of interest)."
        )
    return ""


def judge_invite_problem(event, user):
    """Why `user` (an existing account) cannot become a judge of `event`, or ""."""
    if user.is_platform_admin:
        return "Platform admins can already manage every event."
    held = roles_in(user, event)
    if Role.JUDGE in held:
        return f"{user.email} is already a judge in this event."
    if Role.PARTICIPANT in held:
        return (
            f"{user.email} is competing in this event, so they cannot also judge it "
            "(conflict of interest)."
        )
    return ""


def create_organizer_invite(request, event, email=""):
    """A one-time link that makes its holder a co-organizer of `event` (see create_judge_invite)."""
    return create_judge_invite(request, event, email, role=Role.ORGANIZER)


def create_judge_invite(request, event, email="", tracks=(), role=Role.JUDGE):
    """A one-time link that makes its holder a judge of `event` (or, with `role`, a co-organizer).

    With `email`, only that person can accept it; inviting the same email again revokes their
    pending link first. With no email it is an **open** link that the first person to accept it
    uses up (see JudgeInvite). Returns (invite, raw_token). Only the token's SHA-256 digest is
    stored, so the link can be shown exactly once.
    """
    from accounts.models import digest_token, normalize_email

    email = normalize_email(email) if (email or "").strip() else ""
    existing = User.objects.filter(email=email).first() if email else None
    if existing is not None:
        problem = invite_problem(event, existing, role)
        if problem:
            raise EventRuleError(problem)
    if role != Role.JUDGE:
        tracks = ()
    now = timezone.now()
    raw = (INVITE_PREFIX if role == Role.JUDGE else ORGANIZER_INVITE_PREFIX) + secrets.token_urlsafe(32)
    with transaction.atomic():
        replaced = JudgeInvite.objects.filter(
            event=event, role=role, email=email, accepted_at__isnull=True, revoked_at__isnull=True
        ).update(revoked_at=now) if email else 0
        invite = JudgeInvite.objects.create(
            event=event, role=role, email=email, digest=digest_token(raw), created_by=request.user,
            expires_at=now + INVITE_TTL,
        )
        invite.tracks.set(tracks)
    audit.record(
        _INVITE_ACTIONS[role][0], request=request, subject=event.slug, email=email,
        open_link=not email, tracks=[t.name for t in tracks], expires=invite.expires_at.isoformat(),
        replaced_pending=replaced, has_account=existing is not None,
    )
    return invite, raw


def find_judge_invite(raw):
    """The invite behind a link, whatever its state, or None."""
    from accounts.models import digest_token

    if not raw or not raw.startswith((INVITE_PREFIX, ORGANIZER_INVITE_PREFIX)):
        return None
    return (
        JudgeInvite.objects.select_related("event", "created_by")
        .prefetch_related("tracks").filter(digest=digest_token(raw)).first()
    )


def judge_invite_state(invite, now=None):
    """'open', 'accepted', 'revoked' or 'expired'."""
    if invite.accepted_at:
        return "accepted"
    if invite.revoked_at:
        return "revoked"
    if invite.expires_at <= (now or timezone.now()):
        return "expired"
    return "open"


class _InviteRefused(Exception):
    """Carries a refusal out of the transaction, so its audit row is written after the rollback."""


def accept_judge_invite(request, raw, user):
    """Make `user` a judge of the invite's event, with its tracks, and use the link up.

    Refused -- with an audit row -- unless the link is open, `user` is the invited email, and
    `user` may judge this event. The invite row is locked, so one link is accepted at most once.
    """
    from accounts.models import digest_token

    try:
        with transaction.atomic():
            invite = (
                JudgeInvite.objects.select_for_update(of=("self",)).select_related("event", "created_by")
                .filter(digest=digest_token(raw or "")).first()
            )
            if invite is None:
                raise _InviteRefused(None, "This invite link is not valid.")
            state = judge_invite_state(invite)
            if state != "open":
                raise _InviteRefused(invite, f"This invite link has been {state}.")
            if invite.email and user.email != invite.email:
                raise _InviteRefused(
                    invite, f"This invite is for {invite.email}. Log out, then open the link again."
                )
            problem = invite_problem(invite.event, user, invite.role)
            if problem:
                raise _InviteRefused(invite, problem)
            try:
                with transaction.atomic():
                    membership = EventMembership.objects.create(
                        event=invite.event, user=user, role=invite.role, added_by=invite.created_by
                    )
            except IntegrityError:
                # The conflict-of-interest constraint beat the check above.
                raise _InviteRefused(invite, f"You cannot {'judge' if invite.role == Role.JUDGE else 'organize'} "
                                             "this event (conflict of interest).")
            if invite.role == Role.JUDGE:
                for track in invite.tracks.all():
                    JudgeTrack.objects.create(membership=membership, track=track)
            invite.accepted_at, invite.accepted_by = timezone.now(), user
            invite.save(update_fields=["accepted_at", "accepted_by"])
    except _InviteRefused as refusal:
        invite, reason = refusal.args
        audit.record(
            _INVITE_ACTIONS[invite.role if invite else Role.JUDGE][3], request=request, actor=user,
            subject=invite.event.slug if invite else "", email=user.email, reason=reason,
        )
        raise EventRuleError(reason) from None
    forget_cached_roles(user)
    actions = _INVITE_ACTIONS[invite.role]
    audit.record(
        actions[1], request=request, actor=user, subject=invite.event.slug,
        email=user.email, invited_by=getattr(invite.created_by, "email", ""),
        open_link=not invite.email,
    )
    audit.record(actions[4], request=request, actor=user, subject=invite.event.slug,
                 email=user.email, via="invite link")
    return membership


def revoke_judge_invite(request, event, invite):
    if judge_invite_state(invite) != "open":
        raise EventRuleError("That invite is no longer pending.")
    invite.revoked_at = timezone.now()
    invite.save(update_fields=["revoked_at"])
    audit.record(_INVITE_ACTIONS[invite.role][2], request=request, subject=event.slug,
                 email=invite.email, open_link=not invite.email)


def remove_judge(request, event, membership):
    """Take the judge role away. Their imported scores go with it (Score -> membership)."""
    email = membership.user.email
    membership.delete()
    audit.record(AuditAction.JUDGE_REMOVED, request=request, subject=event.slug, email=email)


# --- deadlines and extensions ---------------------------------------------------------------


def extend_deadline(request, event, new_close, reason):
    """Move the close later for everyone. The timeline stays in order: if the new close reaches
    the judging start, judging (start and end) and the results date, if set, all move later by
    the same amount, so judging keeps its length. The first close is kept in
    `original_submissions_close_at` so pages can say what changed. Refused once judging has
    started, and when the new close is already past."""
    from core.deadlines import db_now

    old_close = event.submissions_close_at
    now = db_now()
    if now >= event.judging_starts_at:
        # Judges are already reviewing: moving the close now would push judging into the future
        # (locking judges out mid-review) and unlock a rubric that reviews were written against.
        raise EventRuleError(
            "Judging has already started, so the submission deadline is final. "
            "To give judges more time, extend judging instead."
        )
    if new_close <= old_close:
        raise EventRuleError("An extension must move the close later. To bring it earlier, edit the settings.")
    if new_close <= now:
        raise EventRuleError("The new close must be in the future, or no team can submit anything.")
    if event.original_submissions_close_at is None:
        event.original_submissions_close_at = old_close
    moved = {}
    if new_close >= event.judging_starts_at:
        shift = new_close - old_close
        for name in ("judging_starts_at", "judging_ends_at", "results_at"):
            old = getattr(event, name)
            if old is not None:
                setattr(event, name, old + shift)
                moved[name] = [old.isoformat(), getattr(event, name).isoformat()]
    event.submissions_close_at = new_close
    event.save(update_fields=[
        "submissions_close_at", "original_submissions_close_at", "judging_starts_at",
        "judging_ends_at", "results_at", "updated_at",
    ])
    audit.record(
        AuditAction.DEADLINE_EXTENDED, request=request, subject=event.slug,
        old=old_close.isoformat(), new=new_close.isoformat(), reason=reason, also_moved=moved,
    )
    return event


def extend_judging(request, event, new_end, reason):
    """Move the judging end later (judges may keep reviewing until then). The first end is kept
    in `original_judging_ends_at`. If a results date is set and the new end reaches it, results
    move by the same amount, so the timeline stays in order."""
    old_end = event.judging_ends_at
    from scoring.models import ResultSnapshot, SnapshotKind

    if ResultSnapshot.objects.filter(event=event, kind=SnapshotKind.FINAL).exists():
        # A final result is a record of judging as it closed; reopening judging under it would
        # make that record describe a judging that never finished.
        audit.record(
            AuditAction.JUDGING_EXTENSION_REFUSED, request=request, subject=event.slug,
            requested=new_end.isoformat(), reason="a final result exists",
        )
        raise EventRuleError(
            "A final result has already been computed for this event, so judging can no longer "
            "be extended."
        )
    if new_end <= old_end:
        raise EventRuleError("An extension must move the judging end later.")
    from core.deadlines import db_now

    if new_end <= db_now():
        raise EventRuleError("The new judging end must be in the future.")
    if event.original_judging_ends_at is None:
        event.original_judging_ends_at = old_end
    moved = {}
    if event.results_at is not None and new_end >= event.results_at:
        old_results = event.results_at
        event.results_at = old_results + (new_end - old_end)
        moved["results_at"] = [old_results.isoformat(), event.results_at.isoformat()]
    event.judging_ends_at = new_end
    event.save(update_fields=["judging_ends_at", "original_judging_ends_at", "results_at", "updated_at"])
    audit.record(
        AuditAction.JUDGING_EXTENDED, request=request, subject=event.slug,
        old=old_end.isoformat(), new=new_end.isoformat(), reason=reason, also_moved=moved,
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
    if until >= event.judging_starts_at:
        raise EventRuleError(
            "An extension must end before judging starts "
            f"({event.judging_starts_at:%Y-%m-%d %H:%M} UTC), so judges never see a moving target."
        )
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
