"""Team formation: create, invite, join, leave, transfer captaincy.

Every function here is a participant write path, so **every one calls the deadline guard**, and the
guard runs before the permission check and before any transaction opens — see `core/guards.py` for
why that ordering matters for the audit trail.

The refusals are deliberately specific. "Invalid invite link" is useless to someone who cannot tell
whether they mistyped the URL, arrived four hours late, or are already on another team. Each
condition below produces its own sentence, and `tests/test_invites.py` asserts they stay distinct.

**Note the transaction shape.** None of these functions is decorated with `@transaction.atomic`.
Guards run first, outside any transaction, and only the writes are wrapped in an explicit
`with transaction.atomic():` block. That is not a style preference: a guard records an audit entry
for what it refused, and a row written inside a transaction that then raises is rolled back with
it. Decorating the whole function would silently discard exactly the evidence the brief requires us
to keep — "every refused write due to the deadline".
"""

from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction

from accounts.models import User
from core import audit, clock
from core.errors import ConflictError, ValidationFailed
from core.guards import guard_permission, guard_submissions_open
from core.models import AuditAction
from core.permissions import can_create_team, can_join_team, can_manage_team, is_team_member
from events.models import Event, Role
from events.services import register_participant
from teams.models import DEFAULT_INVITE_DAYS, Team, TeamInvite, TeamMember

MAX_INVITE_DAYS = 90


class InviteRefused(ConflictError):
    """An invite link that cannot be redeemed, with a reason the recipient can act on."""

    code = "invite_refused"


# --------------------------------------------------------------------------------------
# creating a team
# --------------------------------------------------------------------------------------


def create_team(*, actor: User, event: Event, name: str, request=None) -> Team:
    """Create a team with the caller as captain, registering them as a participant if needed."""
    guard_submissions_open(event, actor=actor, action="create_team", request=request)
    guard_permission(
        can_create_team(actor, event),
        actor=actor,
        action="create_team",
        event=event,
        request=request,
        message="Judges and organizers of an event cannot also compete in it.",
    )

    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Give your team a name.")

    existing = TeamMember.objects.filter(event=event, user=actor).select_related("team").first()
    if existing:
        raise ConflictError(
            f"You are already on “{existing.team.name}” in this event. "
            "Leave that team before creating another."
        )

    with transaction.atomic():
        # Registration first: joining a team without being a participant of the event would leave a
        # membership row and an event role out of step.
        register_participant(user=actor, event=event, request=request)

        team = Team.objects.create(event=event, name=name, created_by=actor)
        TeamMember.objects.create(team=team, user=actor, event=event, is_captain=True)

    audit.record(
        AuditAction.TEAM_CREATED,
        actor=actor,
        event=event,
        target=team,
        metadata={"name": team.name},
        request=request,
    )
    return team


def rename_team(*, actor: User, team: Team, name: str, request=None) -> Team:
    guard_submissions_open(team.event, actor=actor, action="rename_team", target=team, request=request)
    guard_permission(
        can_manage_team(actor, team),
        actor=actor,
        action="rename_team",
        event=team.event,
        target=team,
        request=request,
        message="Only the team captain can rename the team.",
    )

    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Give your team a name.")

    old = team.name
    team.name = name

    with transaction.atomic():
        team.full_clean()
        team.save(update_fields=["name", "updated_at"])

        # The team name carries weight B in the project search vector, so a rename that did not
        # reindex would leave every one of this team's projects findable under the *old* name and
        # invisible under the new one -- a silent, permanent search bug with no error to notice.
        # Imported here rather than at module level to keep the teams app free of a hard
        # dependency on projects.
        from projects.search import rebuild_search_vector

        for project in team.projects.all():
            rebuild_search_vector(project)

    audit.record(
        AuditAction.TEAM_RENAMED,
        actor=actor,
        event=team.event,
        target=team,
        metadata={"renamed_from": old, "renamed_to": name},
        request=request,
    )
    return team


# --------------------------------------------------------------------------------------
# invites
# --------------------------------------------------------------------------------------


def create_invite(
    *,
    actor: User,
    team: Team,
    expires_in_days: int = DEFAULT_INVITE_DAYS,
    max_uses: int | None = None,
    request=None,
) -> tuple[TeamInvite, str]:
    """Mint an invite link. Returns `(invite, plaintext_token)`.

    The plaintext is returned once, for display with a copy button, and only its SHA-256 digest is
    stored — an invite link is a bearer credential, and a leaked database backup should not hand
    over working invitations.

    There is no email delivery anywhere in this portal (the offline rule forbids an SMTP provider),
    so a copyable link *is* the invitation mechanism rather than a fallback.
    """
    guard_submissions_open(team.event, actor=actor, action="create_invite", target=team, request=request)
    guard_permission(
        can_manage_team(actor, team),
        actor=actor,
        action="create_invite",
        event=team.event,
        target=team,
        request=request,
        message="Only the team captain can create invite links.",
    )

    try:
        days = int(expires_in_days)
    except (TypeError, ValueError):
        raise ValidationFailed("Expiry must be a number of days.") from None
    if not 1 <= days <= MAX_INVITE_DAYS:
        raise ValidationFailed(f"Expiry must be between 1 and {MAX_INVITE_DAYS} days.")

    if max_uses is not None:
        try:
            max_uses = int(max_uses)
        except (TypeError, ValueError):
            raise ValidationFailed("Maximum uses must be a whole number.") from None
        if max_uses < 1:
            raise ValidationFailed("Maximum uses must be at least 1, or left blank for unlimited.")

    plaintext = TeamInvite.generate_plaintext()
    invite = TeamInvite.objects.create(
        team=team,
        token_hash=TeamInvite.hash_token(plaintext),
        created_by=actor,
        expires_at=clock.now() + dt.timedelta(days=days),
        max_uses=max_uses,
    )

    # Records that an invite exists and its limits, never the token or its digest.
    audit.record(
        AuditAction.INVITE_CREATED,
        actor=actor,
        event=team.event,
        target=invite,
        metadata={"team": team.name, "expires_at": clock.iso(invite.expires_at), "max_uses": max_uses},
        request=request,
    )
    return invite, plaintext


def revoke_invite(*, actor: User, invite: TeamInvite, request=None) -> TeamInvite:
    guard_submissions_open(
        invite.team.event, actor=actor, action="revoke_invite", target=invite, request=request
    )
    guard_permission(
        can_manage_team(actor, invite.team),
        actor=actor,
        action="revoke_invite",
        event=invite.team.event,
        target=invite,
        request=request,
        message="Only the team captain can revoke invite links.",
    )

    invite.revoke()
    audit.record(
        AuditAction.INVITE_REVOKED,
        actor=actor,
        event=invite.team.event,
        target=invite,
        metadata={"team": invite.team.name},
        request=request,
    )
    return invite


def find_invite(plaintext: str) -> TeamInvite | None:
    """Look an invite up by its token. Returns None for an unknown token.

    Looked up by digest, because the plaintext is not stored.
    """
    if not plaintext:
        return None
    return (
        TeamInvite.objects.select_related("team", "team__event")
        .filter(token_hash=TeamInvite.hash_token(plaintext))
        .first()
    )


def describe_invite_refusal(*, invite: TeamInvite | None, user: User | None) -> str | None:
    """Return the reason this invite cannot be redeemed by this user, or None if it can.

    Split out from `join_via_invite` so the invite *page* can explain the problem before anyone
    clicks a button, using exactly the same rules that will be enforced on the POST. One set of
    conditions, two presentations.

    Order matters: the checks that are about the link come before the checks that are about the
    person, because "this link expired" is more useful than "you are already on a team" when both
    are true.
    """
    if invite is None:
        return "That invite link is not valid. Check you copied the whole link."

    team = invite.team
    event = team.event

    if invite.is_revoked:
        return f"That invite link was revoked by the captain of “{team.name}”."
    if invite.is_expired:
        return (
            f"That invite link expired on {clock.iso(invite.expires_at)}. "
            f"Ask the captain of “{team.name}” for a new one."
        )
    if invite.is_exhausted:
        return (
            f"That invite link has already been used {invite.use_count} "
            f"{'time' if invite.use_count == 1 else 'times'} and cannot be used again. "
            f"Ask the captain of “{team.name}” for a new one."
        )

    from core.deadlines import submissions_are_open

    if not submissions_are_open(event):
        return (
            f"Submissions for {event.name} closed at {clock.iso(event.submissions_close_at)}, "
            "so teams can no longer change."
        )

    if team.is_full:
        return (
            f"“{team.name}” is full: {event.name} allows at most "
            f"{event.max_team_size} members per team."
        )

    if user is None or not user.is_authenticated:
        # Not a refusal: the page will send them to sign in and back again.
        return None

    if is_team_member(user, team):
        return f"You are already a member of “{team.name}”."

    from core.permissions import is_event_staff

    if is_event_staff(user, event):
        return (
            f"You are a judge or organizer of {event.name}, so you cannot join a team competing "
            "in it."
        )

    other = TeamMember.objects.filter(event=event, user=user).select_related("team").first()
    if other:
        return (
            f"You are already on “{other.team.name}” in this event. "
            "You can only belong to one team per event."
        )

    return None


def join_via_invite(*, actor: User, plaintext: str, request=None) -> Team:
    """Redeem an invite link.

    Every condition is checked twice: once here, and again inside the transaction while holding a
    row lock on the invite. The second check is not paranoia -- the link may have been revoked,
    filled or expired in between, and two people may be redeeming the last seat simultaneously.
    The database's unique `(event, user)` index is the final arbiter of that race.

    The audit entry for a refusal is written **after** the transaction has unwound, because a row
    written inside a transaction that then raises is rolled back along with it.
    """
    invite = find_invite(plaintext)
    if invite is None:
        raise InviteRefused(describe_invite_refusal(invite=None, user=actor))

    team = invite.team
    event = team.event

    guard_submissions_open(event, actor=actor, action="join_team", target=team, request=request)
    guard_permission(
        can_join_team(actor, event),
        actor=actor,
        action="join_team",
        event=event,
        target=team,
        request=request,
        message=f"You are a judge or organizer of {event.name}, so you cannot join a team in it.",
    )

    try:
        with transaction.atomic():
            # Lock the invite row so two simultaneous redemptions cannot both read `use_count`
            # before either writes it. Without this, a max_uses=1 link could admit two people.
            locked = TeamInvite.objects.select_for_update().get(pk=invite.pk)

            refusal = describe_invite_refusal(invite=locked, user=actor)
            if refusal:
                raise InviteRefused(refusal)

            register_participant(user=actor, event=event, request=request)

            try:
                TeamMember.objects.create(team=team, user=actor, event=event, is_captain=False)
            except IntegrityError:
                # The unique (event, user) index refused: this caller joined another team in the
                # gap between the check above and this insert. The database caught what a check
                # could not.
                raise InviteRefused(
                    "You joined a team in this event a moment ago, so this link can no longer be "
                    "used."
                ) from None

            locked.use_count += 1
            locked.save(update_fields=["use_count"])
    except InviteRefused as refused:
        # Outside the atomic block, so this row is not rolled back with the attempt it describes.
        audit.record(
            AuditAction.INVITE_REFUSED,
            actor=actor,
            event=event,
            target=invite,
            metadata={"team": team.name, "reason": str(refused)},
            request=request,
        )
        raise

    audit.record(
        AuditAction.TEAM_JOINED,
        actor=actor,
        event=event,
        target=team,
        metadata={"team": team.name, "via": "invite"},
        request=request,
    )
    return team


# --------------------------------------------------------------------------------------
# leaving
# --------------------------------------------------------------------------------------


def leave_team(*, actor: User, team: Team, request=None) -> dict:
    """Leave a team. Returns `{"team_deleted": bool}`.

    Three rules, in order:

    1. **A captain with other members must transfer captaincy first.** Silently promoting someone
       would hand a stranger control of the team and its submission.
    2. **The last member leaving a team whose project is still a draft deletes the team and the
       draft.** An abandoned draft would otherwise sit in the organizer's dashboard forever, and it
       can never be submitted because nobody can edit it.
    3. **Leaving is refused if it would orphan a submitted project.** A submitted project is part of
       the event's record; it must keep an owner who can be contacted and credited.
    """
    guard_submissions_open(team.event, actor=actor, action="leave_team", target=team, request=request)

    membership = TeamMember.objects.filter(team=team, user=actor).first()
    if membership is None:
        raise ConflictError("You are not a member of that team.")

    others = team.members.exclude(pk=membership.pk)
    is_last = not others.exists()

    if membership.is_captain and not is_last:
        raise ConflictError(
            "You are the captain. Transfer captaincy to another member before leaving, so the "
            "team is not left without one."
        )

    if is_last:
        from projects.models import ProjectStatus

        submitted = team.projects.filter(status=ProjectStatus.SUBMITTED)
        if submitted.exists():
            raise ConflictError(
                "You are the last member of this team and it has a submitted project. "
                "Leaving would leave that submission with no owner, so it is not allowed. "
                "Invite someone else to the team first."
                # Deliberately does not offer "ask an organizer to withdraw it". Withdrawal is
                # not implemented -- no code path moves a project from submitted back to draft --
                # and a refusal that names a feature nobody can provide sends the participant to
                # an organizer who can only shrug. See the README's "Not done yet".
            )

    event = team.event
    team_name = team.name
    team_deleted = False
    drafts: list[str] = []

    with transaction.atomic():
        membership.delete()

        if is_last:
            # Only drafts reach here: a submitted project was refused above.
            drafts = list(team.projects.values_list("name", flat=True))
            team.delete()  # cascades to the draft project
            team_deleted = True

            # Their event participation ends with the team: staying a "participant" with no team
            # and no project is a role with nothing attached, and it would block them from being
            # invited as a judge later.
            event.memberships.filter(user=actor, role=Role.PARTICIPANT).delete()

    if team_deleted:
        # Recorded after the commit, and without a `target`: the team row no longer exists, so a
        # foreign key to it would be dangling. The name and the deleted drafts are in the metadata,
        # which is exactly why the audit log stores targets as loose (type, id) strings.
        audit.record(
            AuditAction.TEAM_DELETED,
            actor=actor,
            event=event,
            metadata={"team": team_name, "drafts_deleted": drafts, "reason": "last member left"},
            request=request,
        )

    audit.record(
        AuditAction.TEAM_LEFT,
        actor=actor,
        event=event,
        metadata={"team": team_name, "team_deleted": team_deleted},
        request=request,
    )
    return {"team_deleted": team_deleted}


def transfer_captaincy(*, actor: User, team: Team, new_captain: User, request=None) -> TeamMember:
    """Hand captaincy to another member of the same team."""
    guard_submissions_open(
        team.event, actor=actor, action="transfer_captaincy", target=team, request=request
    )
    guard_permission(
        can_manage_team(actor, team),
        actor=actor,
        action="transfer_captaincy",
        event=team.event,
        target=team,
        request=request,
        message="Only the current captain can transfer captaincy.",
    )

    target = team.members.filter(user=new_captain).first()
    if target is None:
        raise ValidationFailed("That person is not a member of this team.")
    if target.is_captain:
        raise ConflictError("They are already the captain.")

    with transaction.atomic():
        # Demote first. A single-captain-per-team partial unique index means promoting before
        # demoting would hit the constraint, so the order here is load-bearing rather than
        # stylistic -- and both writes must land together, hence the transaction.
        team.members.filter(is_captain=True).update(is_captain=False)
        target.is_captain = True
        target.save(update_fields=["is_captain"])

    audit.record(
        AuditAction.CAPTAIN_TRANSFERRED,
        actor=actor,
        event=team.event,
        target=team,
        metadata={"team": team.name, "to": new_captain.email},
        request=request,
    )
    return target
