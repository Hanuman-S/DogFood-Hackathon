"""Every authorization decision in the portal.

Two kinds of function live here and nothing else does:

* **Policies** — `can_edit_project(user, project)` and friends. Small, named, boolean.
* **Scopers** — `visible_projects(user)` and friends. Return a queryset narrowed to what the
  caller may see.

Views and API endpoints call these. There are no inline role checks anywhere else, and a template
hiding a button is a convenience, never the enforcement. The organizers' rules say a `curl` must
fail for unauthorized access, so the decision has to live where `curl` arrives.

**Roles are per event.** `events.EventMembership` holds participant / judge / organizer for one
event. The only platform-wide flags are `User.is_platform_admin` and `User.can_create_events`.
Asking "is this person a judge?" without naming an event is a category error, and no function here
lets you.

**Deadlines are not authorization.** Nothing in this module consults the clock. "May this person
edit this project?" and "is the submission window open?" are different questions with different
answers and different error codes (403/404 vs 409), and conflating them produces the classic bug
where a late submission is reported as a permission problem. The service layer asks them in order:
`authenticate → resolve event → deadline → permission → validation`.

**404 versus 403.** A caller who is not allowed to know a resource exists gets 404. `can_view_*`
returning False for a draft means "pretend it isn't there", because a 403 on
`/projects/41` confirms that project 41 exists. 403 is for cases where the resource is not a
secret and the refusal is about the action.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from events.models import Role

# --------------------------------------------------------------------------------------
# identity basics
# --------------------------------------------------------------------------------------


def is_authenticated(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False))


def is_admin(user) -> bool:
    """Platform admin. The one role that is not scoped to an event."""
    return is_authenticated(user) and bool(getattr(user, "is_platform_admin", False))


def roles_in(user, event) -> frozenset[str]:
    """Every role this user holds in this event.

    A set rather than a single value because judge + organizer is a legitimate combination (a
    small hackathon's organizer often judges too). Participant can never appear alongside either
    -- a database exclusion constraint enforces that, not just this layer.
    """
    if not is_authenticated(user) or event is None:
        return frozenset()
    return frozenset(
        user.event_memberships.filter(event=event).values_list("role", flat=True)
    )


def is_participant(user, event) -> bool:
    return Role.PARTICIPANT in roles_in(user, event)


def is_judge(user, event) -> bool:
    return Role.JUDGE in roles_in(user, event)


def is_organizer(user, event) -> bool:
    """Organizer **of this event**. Being an organizer elsewhere grants nothing here."""
    return Role.ORGANIZER in roles_in(user, event)


def is_event_staff(user, event) -> bool:
    """Judge or organizer of this event: the two roles that must not also compete in it."""
    return bool(roles_in(user, event) & {Role.JUDGE, Role.ORGANIZER})


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def can_create_event(user) -> bool:
    """Requires the `can_create_events` grant, or platform admin.

    Deliberately not implied by organizing an existing event: running one hackathon does not
    entitle someone to open another on a shared deployment.
    """
    return is_admin(user) or (
        is_authenticated(user) and bool(getattr(user, "can_create_events", False))
    )


def can_manage_event(user, event) -> bool:
    """Edit the event: dates, tracks, prizes, custom questions, memberships.

    Organizers of *this* event, plus platform admins. Judges have no management powers, and
    neither do organizers of other events.
    """
    return is_admin(user) or is_organizer(user, event)


def can_assign_memberships(user, event) -> bool:
    """Add judges or co-organizers. Same authority as managing the event."""
    return can_manage_event(user, event)


def can_view_organizer_dashboard(user, event) -> bool:
    return can_manage_event(user, event)


def can_register_for_event(user, event) -> bool:
    """Join an event as a participant.

    Refused for anyone already on the event's staff: the conflict-of-interest rule says a judge or
    organizer cannot also compete. Already being a participant is not a permission failure, so it
    returns True here and the service layer reports "already registered" instead.
    """
    if not is_authenticated(user) or event is None:
        return False
    return not is_event_staff(user, event)


def can_moderate_projects(user, event) -> bool:
    """Hide a project from the gallery, or flag it as a duplicate.

    Moderation, not authorship -- see `can_edit_project`.
    """
    return can_manage_event(user, event)


# --------------------------------------------------------------------------------------
# teams
# --------------------------------------------------------------------------------------


def can_create_team(user, event) -> bool:
    """Start a team in this event.

    Participants only. Staff are refused by the conflict-of-interest rule, and an unauthenticated
    visitor has nobody to be captain of. Whether the user is *already* on a team is a state
    conflict, not a permission question, and belongs in the service layer where it can produce a
    specific message.
    """
    if not is_authenticated(user) or event is None:
        return False
    return not is_event_staff(user, event)


def can_join_team(user, event) -> bool:
    """Redeem an invite. Same authority as creating a team."""
    return can_create_team(user, event)


def is_team_member(user, team) -> bool:
    if not is_authenticated(user) or team is None:
        return False
    return team.members.filter(user=user).exists()


def is_team_captain(user, team) -> bool:
    if not is_authenticated(user) or team is None:
        return False
    return team.members.filter(user=user, is_captain=True).exists()


def can_manage_team(user, team) -> bool:
    """Create or revoke invite links, and transfer captaincy.

    The captain only. Organizers are deliberately excluded: an organizer issuing invites to
    someone else's team would let them add members to a team they are meant to be judging
    impartially, and there is no workflow that needs it.
    """
    return is_team_captain(user, team)


def can_view_team(user, team) -> bool:
    if team is None:
        return False
    return (
        is_admin(user)
        or is_team_member(user, team)
        or can_manage_event(user, team.event)
    )


# --------------------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------------------


def can_edit_project(user, project) -> bool:
    """Change a project's content, or submit it.

    **Team members only** -- not organizers, and not platform admins.

    That is a deliberate reading of the permission matrix, which lists "no" for both organizer and
    admin on create/edit/submit, against the looser statement elsewhere that an admin can do
    anything. The specific rule wins, because an admin who could rewrite a submission after the
    deadline would undermine the one guarantee this software exists to provide. Admins and
    organizers get full *visibility* and moderation (hide, flag duplicate) instead; they never
    author on a team's behalf.

    The deadline is a separate check -- see the module docstring.
    """
    if not is_authenticated(user) or project is None:
        return False
    return is_team_member(user, project.team)


def can_create_project(user, team) -> bool:
    """Start a project for this team. Same authority as editing one."""
    if not is_authenticated(user) or team is None:
        return False
    return is_team_member(user, team)


def can_view_project(user, project) -> bool:
    """Whether this project may be shown at all.

    False means the view answers **404**, not 403: a draft's existence is itself private.

    Publicly visible projects are visible to everyone including anonymous visitors. Everything
    else -- drafts, organizer-hidden projects, flagged duplicates, and projects in an event whose
    gallery is switched off -- is restricted to the owning team, the event's organizers, and
    platform admins.
    """
    if project is None:
        return False

    if _is_publicly_visible(project):
        return True

    return (
        is_admin(user)
        or is_team_member(user, project.team)
        or is_organizer(user, project.event)
    )


def _is_publicly_visible(project) -> bool:
    """The gallery's four conditions, applied to a single object.

    Mirrors `projects.models.ProjectQuerySet.gallery_visible`. The queryset version is what runs
    for list pages; this is for a single already-fetched object. `tests/test_permissions.py`
    asserts the two agree for every project in the fixture set, because a drift between them is
    precisely how a draft leaks onto a detail page that the list page correctly hid.
    """
    from projects.models import ProjectStatus

    return (
        project.status == ProjectStatus.SUBMITTED
        and project.duplicate_of_id is None
        and not project.hidden_by_organizer
        and project.event.gallery_public
    )


def can_view_project_scores(user, project) -> bool:
    """Nobody, in T1.

    Scores are imported and stored, and no T1 surface displays them -- not the gallery, not the
    project page, not the organizer dashboard. T2 replaces this with real judge isolation. It
    returns False rather than being absent so that any T1 template or serializer that reaches for
    scores has to go through a function that says "no", and so T2 has one obvious place to
    implement the rule.
    """
    return False


# --------------------------------------------------------------------------------------
# queryset scopers
# --------------------------------------------------------------------------------------


def visible_projects(user) -> QuerySet:
    """Every project this caller may see, as a queryset.

    The list-page counterpart to `can_view_project`. Used by the gallery, the API and the
    organizer dashboard, so all three answer the same question.
    """
    from projects.models import Project

    public = Project.objects.gallery_visible()

    if not is_authenticated(user):
        return public
    if is_admin(user):
        return Project.objects.all()

    # A participant sees their own team's work (including drafts); an organizer sees everything in
    # the events they run. `distinct()` because the OR can match a row through more than one
    # branch.
    return Project.objects.filter(
        Q(pk__in=public.values("pk"))
        | Q(team__members__user=user)
        | Q(event__memberships__user=user, event__memberships__role=Role.ORGANIZER)
    ).distinct()


def gallery_projects() -> QuerySet:
    """The public gallery, for everyone, regardless of who is asking.

    Separate from `visible_projects` on purpose. The gallery must show the same thing to a logged
    in participant as to a stranger -- otherwise a team sees their own draft in the listing,
    assumes it is public, and does not submit it.
    """
    from projects.models import Project

    return Project.objects.gallery_visible()


def visible_events(user) -> QuerySet:
    from events.models import Event

    return Event.objects.visible_to(user)


def manageable_events(user) -> QuerySet:
    """Events this caller may administer: their own, or all of them for a platform admin."""
    from events.models import Event

    if not is_authenticated(user):
        return Event.objects.none()
    if is_admin(user):
        return Event.objects.all()
    return Event.objects.filter(
        memberships__user=user, memberships__role=Role.ORGANIZER
    ).distinct()


def visible_teams(user) -> QuerySet:
    from teams.models import Team

    if not is_authenticated(user):
        return Team.objects.none()
    if is_admin(user):
        return Team.objects.all()
    return Team.objects.filter(
        Q(members__user=user)
        | Q(event__memberships__user=user, event__memberships__role=Role.ORGANIZER)
    ).distinct()
