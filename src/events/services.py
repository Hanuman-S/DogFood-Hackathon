"""Event, track, prize, question and membership operations.

Organizer actions, plus participant registration. Every write goes through here so that the UI and
any future API endpoint enforce the same rules and write the same audit entries.

**These are not guarded by the submission deadline.** The brief lists the guarded paths, and none of
them is an organizer action: an organizer must be able to fix a track name, add a prize or *extend*
`submissions_close_at` after the window has shut, which is the entire point of being able to extend
it. Participant registration is likewise unguarded, deliberately -- the brief's list does not
include it, and registering for a closed event is harmless (it grants no ability to submit, because
that path *is* guarded).

**Transaction shape.** No function here is decorated with `@transaction.atomic`. Guards run first,
outside any transaction, because a guard writes an audit entry recording what it refused and a row
written inside a transaction that then raises is rolled back with it -- which would discard exactly
the evidence the audit trail exists to keep. Where several writes genuinely must land together,
there is an explicit `with transaction.atomic():` block around those writes only.
"""

from __future__ import annotations

from django.db import transaction

from accounts.models import User
from core import audit, clock
from core.errors import ConflictError, ValidationFailed
from core.guards import guard_permission
from core.models import AuditAction
from core.permissions import (
    can_assign_memberships,
    can_create_event,
    can_manage_event,
    can_register_for_event,
)
from events.models import CustomQuestion, Event, EventMembership, JudgeTrack, Prize, Role, Track

# Fields whose change is recorded as a date change, with old and new values, because moving a
# deadline is the single most consequential edit an organizer can make.
DATE_FIELDS = ("starts_at", "submissions_open_at", "submissions_close_at", "judging_ends_at")


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def create_event(*, actor: User, request=None, **fields) -> Event:
    """Create an event. The creator becomes its first organizer.

    That membership is created here rather than left to a later step: an event with no organizer is
    unmanageable, and the only person who could fix it would be a platform admin.
    """
    guard_permission(
        can_create_event(actor),
        actor=actor,
        action="create_event",
        request=request,
        message="You do not have permission to create events. Ask an administrator for access.",
    )

    fields.setdefault("created_by", actor)
    event = Event(**fields)
    if not event.slug:
        event.slug = Event.build_slug(event.name)
    event.full_clean()

    # Both writes or neither: an event that exists with no organizer is unmanageable by anyone
    # except a platform admin.
    with transaction.atomic():
        event.save()
        EventMembership.objects.create(user=actor, event=event, role=Role.ORGANIZER)

    audit.record(
        AuditAction.EVENT_CREATED,
        actor=actor,
        event=event,
        target=event,
        metadata={"slug": event.slug, "name": event.name},
        request=request,
    )
    return event


def update_event(*, actor: User, event: Event, request=None, **fields) -> Event:
    """Edit an event, recording any date change with its old and new values.

    Allowed after the window has closed. Extending `submissions_close_at` is explicitly permitted --
    an organizer needs it when a power cut or a broken CI eats the last hour of a hackathon -- and
    the audit entry is what makes that accountable rather than invisible.
    """
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="update_event",
        event=event,
        target=event,
        request=request,
    )

    before = {name: getattr(event, name) for name in fields}

    for name, value in fields.items():
        setattr(event, name, value)
    event.full_clean()
    event.save()

    changed = {name: value for name, value in fields.items() if before[name] != value}

    date_changes = {
        name: {"from": clock.iso(before[name]), "to": clock.iso(getattr(event, name))}
        for name in changed
        if name in DATE_FIELDS
    }
    if date_changes:
        audit.record(
            AuditAction.EVENT_DATES_CHANGED,
            actor=actor,
            event=event,
            target=event,
            metadata={"changes": date_changes},
            request=request,
        )

    other_changes = sorted(set(changed) - set(DATE_FIELDS))
    if other_changes:
        audit.record(
            AuditAction.EVENT_UPDATED,
            actor=actor,
            event=event,
            target=event,
            metadata={"fields": other_changes},
            request=request,
        )

    return event


# --------------------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------------------


def save_track(*, actor: User, event: Event, track: Track | None = None, request=None, **fields) -> Track:
    """Create or edit a track."""
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="save_track",
        event=event,
        request=request,
    )

    # Captured before the save, because afterwards the instance always has a primary key.
    creating = track is None
    if creating:
        track = Track(event=event)
    for name, value in fields.items():
        setattr(track, name, value)
    track.full_clean()
    track.save()

    audit.record(
        AuditAction.TRACK_CHANGED,
        actor=actor,
        event=event,
        target=track,
        metadata={"name": track.name, "created": creating},
        request=request,
    )
    return track


def delete_track(*, actor: User, track: Track, request=None) -> None:
    """Delete a track, unless a project references it.

    `Project.track` is `PROTECT`, so the database would refuse anyway -- but it would refuse with an
    `IntegrityError`, which reaches an organizer as a 500. Checking first turns that into a sentence
    they can act on. The constraint stays as the backstop against a path that forgets to check.
    """
    event = track.event
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="delete_track",
        event=event,
        target=track,
        request=request,
    )

    in_use = track.projects.count()
    if in_use:
        raise ConflictError(
            f"“{track.name}” cannot be deleted: {in_use} "
            f"{'project' if in_use == 1 else 'projects'} already reference it. "
            "Move those projects to another track first, or rename this one instead."
        )

    if JudgeTrack.objects.filter(track=track).exists():
        # Cascades rather than blocks, so this is a warning in the audit entry, not a refusal:
        # unassigning judges from a track that no longer exists is the correct outcome.
        audit.record(
            AuditAction.TRACK_CHANGED,
            actor=actor,
            event=event,
            target=track,
            metadata={"name": track.name, "deleted": True, "judge_assignments_removed": True},
            request=request,
        )
    else:
        audit.record(
            AuditAction.TRACK_CHANGED,
            actor=actor,
            event=event,
            target=track,
            metadata={"name": track.name, "deleted": True},
            request=request,
        )

    track.delete()


def reorder(*, actor: User, event: Event, model, ordered_ids: list[int], request=None) -> None:
    """Apply an explicit display order to tracks, prizes or questions.

    One function for all three because "set `order` from the position in this list" is the whole
    operation, and three near-identical copies would be three places for it to drift.
    """
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action=f"reorder_{model._meta.model_name}",
        event=event,
        request=request,
    )

    # Scoped to this event, so an id from someone else's event cannot be reordered into it.
    owned = {obj.pk: obj for obj in model.objects.filter(event=event, pk__in=ordered_ids)}

    # One transaction: a half-applied reordering leaves duplicate `order` values, which makes the
    # displayed sequence arbitrary rather than merely wrong.
    with transaction.atomic():
        for position, pk in enumerate(ordered_ids):
            obj = owned.get(pk)
            if obj is not None and obj.order != position:
                obj.order = position
                obj.save(update_fields=["order"])


# --------------------------------------------------------------------------------------
# prizes
# --------------------------------------------------------------------------------------


def save_prize(*, actor: User, event: Event, prize: Prize | None = None, request=None, **fields) -> Prize:
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="save_prize",
        event=event,
        request=request,
    )

    if prize is None:
        prize = Prize(event=event)
    for name, value in fields.items():
        setattr(prize, name, value)

    if prize.track_id and prize.track.event_id != event.pk:
        raise ValidationFailed("That track belongs to a different event.")

    prize.full_clean()
    prize.save()

    audit.record(
        AuditAction.PRIZE_CHANGED,
        actor=actor,
        event=event,
        target=prize,
        metadata={"name": prize.name},
        request=request,
    )
    return prize


def delete_prize(*, actor: User, prize: Prize, request=None) -> None:
    """Prizes have no dependents, so this always succeeds for an organizer."""
    event = prize.event
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="delete_prize",
        event=event,
        target=prize,
        request=request,
    )
    audit.record(
        AuditAction.PRIZE_CHANGED,
        actor=actor,
        event=event,
        target=prize,
        metadata={"name": prize.name, "deleted": True},
        request=request,
    )
    prize.delete()


# --------------------------------------------------------------------------------------
# custom questions
# --------------------------------------------------------------------------------------


def save_question(
    *, actor: User, event: Event, question: CustomQuestion | None = None, request=None, **fields
) -> CustomQuestion:
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="save_question",
        event=event,
        request=request,
    )

    creating = question is None
    if creating:
        question = CustomQuestion(event=event)

    # Changing the kind of a question that already has answers would silently reinterpret them --
    # a "yes" stored for a boolean becomes a meaningless short-text answer. Refuse instead.
    if not creating and "kind" in fields and fields["kind"] != question.kind:
        answered = question.answers.count()
        if answered:
            raise ConflictError(
                f"This question already has {answered} "
                f"{'answer' if answered == 1 else 'answers'}, so its type cannot be changed. "
                "Add a new question instead."
            )

    for name, value in fields.items():
        setattr(question, name, value)
    question.full_clean()
    question.save()

    audit.record(
        AuditAction.QUESTION_CHANGED,
        actor=actor,
        event=event,
        target=question,
        metadata={"prompt": question.prompt, "kind": question.kind, "created": creating},
        request=request,
    )
    return question


def delete_question(*, actor: User, question: CustomQuestion, request=None) -> None:
    """Delete a question, unless projects have already answered it.

    `CustomAnswer` cascades from the question, so deleting one would quietly destroy every team's
    answer to it. That is data an organizer asked for and teams took the trouble to write, so the
    deletion is refused rather than allowed to shred it.
    """
    event = question.event
    guard_permission(
        can_manage_event(actor, event),
        actor=actor,
        action="delete_question",
        event=event,
        target=question,
        request=request,
    )

    answered = question.answers.count()
    if answered:
        raise ConflictError(
            f"“{question.prompt}” cannot be deleted: {answered} "
            f"{'project has' if answered == 1 else 'projects have'} already answered it. "
            "Deleting it would destroy those answers. Make it optional instead if you no longer "
            "need it."
        )

    audit.record(
        AuditAction.QUESTION_CHANGED,
        actor=actor,
        event=event,
        target=question,
        metadata={"prompt": question.prompt, "deleted": True},
        request=request,
    )
    question.delete()


# --------------------------------------------------------------------------------------
# memberships
# --------------------------------------------------------------------------------------


def add_membership(
    *,
    actor: User,
    event: Event,
    email: str,
    role: str,
    track_ids: list[int] | None = None,
    request=None,
) -> EventMembership:
    """Add an existing account to an event as a judge or co-organizer.

    By email, because that is the only identifier an organizer has for someone else. The account
    must already exist: there is no email delivery in this portal, so an invitation cannot be sent,
    and silently creating an account the person cannot log into would be worse than saying so.
    """
    guard_permission(
        can_assign_memberships(actor, event),
        actor=actor,
        action="add_membership",
        event=event,
        request=request,
    )

    if role not in (Role.JUDGE, Role.ORGANIZER):
        raise ValidationFailed(
            "Only judges and organizers are assigned by an organizer. Participants register "
            "themselves."
        )

    email = (email or "").strip().lower()
    user = User.objects.filter(email=email).first()
    if user is None:
        raise ValidationFailed(
            f"No account exists for {email}. Ask them to sign up first — this deployment sends no "
            "email, so an invitation cannot be delivered."
        )

    # The conflict-of-interest rule, reported as a sentence. The database enforces it too, via an
    # exclusion constraint; without this check the organizer would see a 500.
    if event.memberships.filter(user=user, role=Role.PARTICIPANT).exists():
        raise ConflictError(
            f"{email} is already a participant in this event and cannot also be a "
            f"{role}. They would have to leave their team first."
        )

    # A judge created without their track assignments is a judge nobody can assign work to, so the
    # membership and its tracks land together.
    with transaction.atomic():
        membership, created = EventMembership.objects.get_or_create(
            user=user, event=event, role=role
        )

        if role == Role.JUDGE:
            set_judge_tracks(
                actor=actor, membership=membership, track_ids=track_ids or [], request=request
            )

    if created:
        audit.record(
            AuditAction.MEMBERSHIP_ADDED,
            actor=actor,
            event=event,
            target=membership,
            metadata={"email": email, "role": role},
            request=request,
        )
    return membership


def set_judge_tracks(
    *, actor: User, membership: EventMembership, track_ids: list[int], request=None
) -> None:
    """Replace a judge's track assignments.

    Replace rather than add, so the UI can present it as a set of checkboxes and unchecking one
    actually removes it.
    """
    event = membership.event
    guard_permission(
        can_assign_memberships(actor, event),
        actor=actor,
        action="set_judge_tracks",
        event=event,
        target=membership,
        request=request,
    )

    if membership.role != Role.JUDGE:
        raise ValidationFailed("Only a judge membership can have tracks.")

    # Scoped to this event's tracks: an id from another event must not be assignable.
    tracks = list(Track.objects.filter(event=event, pk__in=track_ids))

    # Replace, atomically: between the delete and the re-add, a judge would briefly have no tracks
    # at all, and a crash in that window would leave them unassigned.
    with transaction.atomic():
        JudgeTrack.objects.filter(membership=membership).exclude(track__in=tracks).delete()
        for track in tracks:
            JudgeTrack.objects.get_or_create(membership=membership, track=track)


def remove_membership(*, actor: User, membership: EventMembership, request=None) -> None:
    """Remove a judge or co-organizer from an event.

    Refuses to remove the last organizer: an event nobody can manage is only fixable by a platform
    admin, and it is an easy mistake to make when tidying up a membership list.
    """
    event = membership.event
    guard_permission(
        can_assign_memberships(actor, event),
        actor=actor,
        action="remove_membership",
        event=event,
        target=membership,
        request=request,
    )

    if membership.role == Role.ORGANIZER:
        remaining = event.memberships.filter(role=Role.ORGANIZER).exclude(pk=membership.pk).count()
        if remaining == 0:
            raise ConflictError(
                "This is the event's only organizer. Add another organizer before removing this "
                "one, or the event would be left unmanageable."
            )

    if membership.role == Role.PARTICIPANT:
        raise ConflictError(
            "Participants are not removed here. They leave their team, which ends their "
            "participation."
        )

    audit.record(
        AuditAction.MEMBERSHIP_REMOVED,
        actor=actor,
        event=event,
        target=membership,
        metadata={"email": membership.user.email, "role": membership.role},
        request=request,
    )
    membership.delete()


def register_participant(*, user: User, event: Event, request=None) -> EventMembership:
    """Register the caller as a participant.

    Not guarded by the deadline: the brief's list of guarded paths does not include registration,
    and registering for a closed event grants nothing, because creating a team and submitting a
    project are both guarded.

    Idempotent -- registering twice is not an error, it is a double-click.
    """
    guard_permission(
        can_register_for_event(user, event),
        actor=user,
        action="register_participant",
        event=event,
        request=request,
        message=(
            "You are a judge or organizer of this event, so you cannot also compete in it."
        ),
    )

    membership, created = EventMembership.objects.get_or_create(
        user=user, event=event, role=Role.PARTICIPANT
    )
    if created:
        audit.record(
            AuditAction.REGISTERED,
            actor=user,
            event=event,
            target=membership,
            request=request,
        )
    return membership


# --------------------------------------------------------------------------------------
# dashboard data
# --------------------------------------------------------------------------------------


def dashboard_counts(event: Event) -> dict:
    """Counts for the organizer dashboard.

    Computed with aggregate queries rather than by iterating, because an event with 40 teams and 41
    projects is the *small* case and the fixture proves the shape.
    """
    from projects.models import Project, ProjectStatus
    from teams.models import Team

    projects = Project.objects.filter(event=event)
    return {
        "participants": event.memberships.filter(role=Role.PARTICIPANT).count(),
        "judges": event.memberships.filter(role=Role.JUDGE).count(),
        "organizers": event.memberships.filter(role=Role.ORGANIZER).count(),
        "teams": Team.objects.filter(event=event).count(),
        "drafts": projects.filter(status=ProjectStatus.DRAFT).count(),
        "submitted": projects.filter(status=ProjectStatus.SUBMITTED).count(),
        "duplicates": projects.filter(duplicate_of__isnull=False).count(),
        "hidden": projects.filter(hidden_by_organizer=True).count(),
        "in_gallery": projects.gallery_visible().count(),
    }
