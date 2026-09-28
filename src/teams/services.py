"""Every write to a team. The rules table is in teams/models.py."""

from django.db import IntegrityError, transaction
from django.db.models import RestrictedError

from accounts.roles import Role, can_compete_in, forget_cached_roles
from core import audit
from core.deadlines import check_submission_window
from core.models import AuditAction
from events.models import EventMembership
from teams.models import Team, TeamMember, new_invite_token


class TeamRuleError(Exception):
    """A refused team action, with a sentence the UI can show as-is."""


def team_of(user, event):
    """The user's team in `event`, or None."""
    if not user.is_authenticated:
        return None
    membership = TeamMember.objects.filter(event=event, user=user).select_related("team").first()
    return membership.team if membership else None


def is_member(user, team):
    return user.is_authenticated and team.members.filter(user=user).exists()


def _require_can_compete(user, event):
    # The service-layer half of the conflict-of-interest rule: nobody who is staff in this event
    # (judge or organizer), and no platform admin, may be on one of its teams. The exclusion
    # constraint on events_eventmembership is the database half.
    problem = can_compete_in(user, event)
    if problem:
        raise TeamRuleError(problem)


def _register(user, event):
    """Joining or creating a team is what registers a participant in an event (design T5)."""
    EventMembership.objects.get_or_create(user=user, event=event, role=Role.PARTICIPANT)
    forget_cached_roles(user)


def _unregister(user, event):
    """Off every team in the event -> no longer a participant in it."""
    if not TeamMember.objects.filter(event=event, user=user).exists():
        EventMembership.objects.filter(user=user, event=event, role=Role.PARTICIPANT).delete()
        forget_cached_roles(user)


def _require_published(event):
    if not event.is_published:
        raise TeamRuleError("This event is not open to participants yet.")


def _unique_name(event, wanted):
    """`wanted`, or `wanted (2)`, `wanted (3)`… whichever is free in this event."""
    name, n = wanted[:80], 2
    while Team.objects.filter(event=event, name__iexact=name).exists():
        suffix = f" ({n})"
        name = wanted[: 80 - len(suffix)] + suffix
        n += 1
    return name


def create_team(request, event, name, *, solo=False):
    user = request.user
    check_submission_window(request, event, None, action="create a team")
    _require_can_compete(user, event)
    _require_published(event)
    if team_of(user, event):
        raise TeamRuleError("You are already on a team in this event.")
    name = name.strip()
    if not name:
        raise TeamRuleError("Give the team a name.")
    if not solo and Team.objects.filter(event=event, name__iexact=name).exists():
        raise TeamRuleError("Another team in this event already has that name.")
    try:
        with transaction.atomic():
            team = Team.objects.create(
                event=event, name=_unique_name(event, name) if solo else name, captain=user
            )
            TeamMember.objects.create(team=team, user=user)
            _register(user, event)
    except IntegrityError as error:  # a double-click, or the exclusion constraint
        raise TeamRuleError("You are already on a team in this event.") from error
    audit.record(AuditAction.TEAM_CREATED, request=request, subject=team.name, event=event.slug, solo=solo)
    return team


def solo_team(request, event):
    """A team of one for a participant who starts a project alone."""
    return create_team(request, event, f"{request.user.name or request.user.email}", solo=True)


def join_problem(user, team):
    """Why `user` cannot join `team` right now, as a sentence -- or "" if they can."""
    event = team.event
    problem = can_compete_in(user, event)
    if problem:
        return problem
    if not event.is_published:
        return "This event is not open to participants yet."
    current = team_of(user, event)
    if current == team:
        return "You are already on this team."
    if current:
        return f"You are already on another team in this event ({current.name}). Leave it first."
    if team.is_full():
        return f"This team is full ({event.max_team_size} members)."
    return ""


def join_team(request, token):
    user = request.user
    team = Team.objects.select_related("event").filter(invite_token=token).first()
    if team is None:
        raise TeamRuleError("This invite link is not valid. It may have been replaced.")
    check_submission_window(request, team.event, team, action="join a team")
    try:
        with transaction.atomic():
            # Lock the team row so two people clicking the link at once cannot both take the
            # last seat: the second waits, then sees the team full.
            team = Team.objects.select_for_update().select_related("event").get(pk=team.pk)
            problem = join_problem(user, team)
            if problem:
                raise TeamRuleError(problem)
            TeamMember.objects.create(team=team, user=user)
            _register(user, team.event)
    except TeamRuleError as error:
        audit.record(AuditAction.TEAM_JOIN_REFUSED, request=request, subject=team.name, event=team.event.slug, reason=str(error))
        raise
    except IntegrityError as error:
        raise TeamRuleError("You are already on a team in this event.") from error
    audit.record(AuditAction.TEAM_JOINED, request=request, subject=team.name, event=team.event.slug)
    return team


def _require_captain(user, team):
    if team.captain_id != user.pk:
        raise TeamRuleError("Only the team captain can do that.")


def rename_team(request, team, name):
    check_submission_window(request, team.event, team, action="rename the team")
    _require_captain(request.user, team)
    name = name.strip()
    if not name:
        raise TeamRuleError("Give the team a name.")
    if Team.objects.filter(event=team.event, name__iexact=name).exclude(pk=team.pk).exists():
        raise TeamRuleError("Another team in this event already has that name.")
    old = team.name
    team.name = name[:80]
    team.save(update_fields=["name"])
    audit.record(AuditAction.TEAM_RENAMED, request=request, subject=team.name, event=team.event.slug, old=old)


def reset_invite_link(request, team):
    check_submission_window(request, team.event, team, action="replace the invite link")
    _require_captain(request.user, team)
    team.invite_token = new_invite_token()
    team.save(update_fields=["invite_token"])
    audit.record(AuditAction.TEAM_LINK_RESET, request=request, subject=team.name, event=team.event.slug)


def transfer_captain(request, team, member):
    check_submission_window(request, team.event, team, action="hand over captaincy")
    _require_captain(request.user, team)
    if member.team_id != team.pk:
        raise TeamRuleError("That person is not on this team.")
    team.captain = member.user
    team.save(update_fields=["captain"])
    audit.record(AuditAction.TEAM_CAPTAIN_CHANGED, request=request, subject=team.name, event=team.event.slug, to=member.user.email)


def _require_size_kept(team):
    """A submitted project's team may not shrink below the event minimum: that would turn a
    valid submission into an invalid one behind the organizers' backs."""
    project = getattr(team, "project", None)
    if project is not None and project.is_submitted and team.members.count() - 1 < team.event.min_team_size:
        raise TeamRuleError(
            f"Your project is submitted and this event needs at least {team.event.min_team_size} "
            "members. Withdraw it to draft first."
        )


def remove_member(request, team, member):
    check_submission_window(request, team.event, team, action="remove a member")
    _require_captain(request.user, team)
    _require_size_kept(team)
    if member.user_id == team.captain_id:
        raise TeamRuleError("The captain cannot remove themselves. Hand over captaincy, then leave.")
    email = member.user.email
    with transaction.atomic():
        member.delete()
        _unregister(member.user, team.event)
    audit.record(AuditAction.TEAM_MEMBER_REMOVED, request=request, subject=team.name, event=team.event.slug, email=email)


def leave_team(request, team):
    """Leave. The captain must hand over first, unless they are the last member -- in which
    case the team is disbanded along with its draft. A submitted project is never deleted this
    way: withdraw it to draft first."""
    user = request.user
    check_submission_window(request, team.event, team, action="leave the team")
    membership = team.members.filter(user=user).first()
    if membership is None:
        raise TeamRuleError("You are not on this team.")
    others = team.members.exclude(user=user).count()
    if others and team.captain_id == user.pk:
        raise TeamRuleError("You are the captain. Hand over captaincy before leaving.")
    if others:
        _require_size_kept(team)
    if not others:
        project = getattr(team, "project", None)
        if project is not None and project.is_submitted:
            raise TeamRuleError(
                "You are the last member and the project is submitted. Withdraw it to draft first "
                "if you really want to delete the team."
            )
        name, event = team.name, team.event
        try:
            with transaction.atomic():
                team.delete()  # cascades to the membership and any draft project
                _unregister(user, event)
        except RestrictedError:
            # Score.project is RESTRICT: a project with reviews (submitted or draft) never goes with its team.
            raise TeamRuleError(
                "The project has reviews from judges, so the team and its project cannot be deleted.") from None
        audit.record(AuditAction.TEAM_DISBANDED, request=request, subject=name, event=event.slug)
        return None
    with transaction.atomic():
        membership.delete()
        _unregister(user, team.event)
    audit.record(AuditAction.TEAM_LEFT, request=request, subject=team.name, event=team.event.slug)
    return team
