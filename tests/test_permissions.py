"""The permission layer.

Two halves:

1. **The policy functions** in `core.permissions`, tested directly. Fast, exhaustive, and the right
   place to pin down the awkward cases (judge+organizer allowed, admin cannot author a submission).
2. **Agreement between the object-level and queryset-level answers.** `can_view_project` and
   `visible_projects` must never disagree, because a drift between them is exactly how a draft ends
   up hidden from the gallery listing but readable on its own detail page.

The matrix from the brief is also exercised against real URLs; those tests live alongside the views
they hit (`test_auth.py` here, and the event/team/project/gallery test modules as those land), so a
failure names the endpoint rather than a policy function.
"""

from __future__ import annotations

import pytest

from core import permissions as perms
from events.models import Role
from projects.models import Project, ProjectStatus
from tests.factories import (
    add_team_member,
    make_admin,
    make_event,
    make_judge,
    make_membership,
    make_organizer,
    make_project,
    make_submitted_project,
    make_team,
    make_track,
    make_user,
)


@pytest.fixture
def world(db):
    """One event with every role represented, plus an unrelated second event.

    The second event exists so that "organizer of *an* event" can never be mistaken for
    "organizer of *this* event" -- the most likely way a permission bug hides.
    """
    event = make_event(name="Main Event")
    other_event = make_event(name="Other Event")

    team = make_team(event, name="Team A")
    captain = team.captain().user
    teammate = add_team_member(team).user

    rival_team = make_team(event, name="Team B")
    rival = rival_team.captain().user

    return {
        "event": event,
        "other_event": other_event,
        "team": team,
        "captain": captain,
        "teammate": teammate,
        "rival_team": rival_team,
        "rival": rival,
        "judge": make_judge(event),
        "organizer": make_organizer(event),
        "other_organizer": make_organizer(other_event),
        "admin": make_admin(),
        "outsider": make_user(),
        "anonymous": None,
    }


# --------------------------------------------------------------------------------------
# roles are per event
# --------------------------------------------------------------------------------------


def test_roles_are_scoped_to_one_event(world):
    event, other = world["event"], world["other_event"]

    assert perms.is_organizer(world["organizer"], event)
    assert not perms.is_organizer(world["organizer"], other)

    assert perms.is_organizer(world["other_organizer"], other)
    assert not perms.is_organizer(world["other_organizer"], event)

    assert perms.is_participant(world["captain"], event)
    assert not perms.is_participant(world["captain"], other)


def test_an_anonymous_caller_holds_no_roles(world):
    for check in (perms.is_participant, perms.is_judge, perms.is_organizer, perms.is_event_staff):
        assert check(None, world["event"]) is False


def test_judge_and_organizer_can_be_held_together(db):
    """A small hackathon's organizer often judges too, so this must be allowed."""
    event = make_event()
    person = make_user()
    make_membership(person, event, Role.JUDGE)
    make_membership(person, event, Role.ORGANIZER)

    assert perms.roles_in(person, event) == frozenset({Role.JUDGE, Role.ORGANIZER})
    assert perms.is_event_staff(person, event)
    assert not perms.is_participant(person, event)


def test_admin_is_not_event_scoped(world):
    """The one role that is platform-wide."""
    assert perms.is_admin(world["admin"])
    assert not perms.is_admin(world["organizer"])
    assert not perms.is_admin(None)


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def test_only_granted_users_and_admins_can_create_events(world, db):
    assert perms.can_create_event(world["admin"])
    assert perms.can_create_event(world["organizer"])  # factory grants can_create_events
    assert not perms.can_create_event(world["captain"])
    assert not perms.can_create_event(world["outsider"])
    assert not perms.can_create_event(None)

    granted = make_user(can_create_events=True)
    assert perms.can_create_event(granted)


def test_organizing_one_event_does_not_grant_creating_another(db):
    """Running a hackathon should not let you open new ones on a shared deployment."""
    event = make_event()
    organizer = make_user()  # no can_create_events grant
    make_membership(organizer, event, Role.ORGANIZER)

    assert perms.can_manage_event(organizer, event)
    assert not perms.can_create_event(organizer)


def test_only_the_events_own_organizer_or_an_admin_can_manage_it(world):
    event = world["event"]
    assert perms.can_manage_event(world["organizer"], event)
    assert perms.can_manage_event(world["admin"], event)

    for who in ("judge", "captain", "outsider", "other_organizer", "anonymous"):
        assert not perms.can_manage_event(world[who], event), who


def test_assigning_memberships_needs_the_same_authority_as_managing(world):
    event = world["event"]
    assert perms.can_assign_memberships(world["organizer"], event)
    assert perms.can_assign_memberships(world["admin"], event)
    assert not perms.can_assign_memberships(world["judge"], event)
    assert not perms.can_assign_memberships(world["captain"], event)


def test_event_staff_cannot_register_as_participants(world):
    """The conflict-of-interest rule, at the permission layer.

    The database enforces it too; this is what produces a readable refusal instead of an
    IntegrityError.
    """
    event = world["event"]
    assert not perms.can_register_for_event(world["judge"], event)
    assert not perms.can_register_for_event(world["organizer"], event)
    assert not perms.can_register_for_event(None, event)

    # An outsider may register; so may someone who is staff on a *different* event.
    assert perms.can_register_for_event(world["outsider"], event)
    assert perms.can_register_for_event(world["other_organizer"], event)


# --------------------------------------------------------------------------------------
# teams
# --------------------------------------------------------------------------------------


def test_only_non_staff_can_create_or_join_teams(world):
    event = world["event"]
    assert perms.can_create_team(world["outsider"], event)
    assert perms.can_join_team(world["outsider"], event)

    for who in ("judge", "organizer", "anonymous"):
        assert not perms.can_create_team(world[who], event), who
        assert not perms.can_join_team(world[who], event), who


def test_only_the_captain_manages_a_team(world):
    team = world["team"]
    assert perms.can_manage_team(world["captain"], team)
    assert not perms.can_manage_team(world["teammate"], team)
    # Deliberately excluded: an organizer issuing invites to a team they will judge is a
    # conflict, and no workflow needs it.
    assert not perms.can_manage_team(world["organizer"], team)
    assert not perms.can_manage_team(world["admin"], team)


def test_team_visibility(world):
    team = world["team"]
    assert perms.can_view_team(world["captain"], team)
    assert perms.can_view_team(world["teammate"], team)
    assert perms.can_view_team(world["organizer"], team)
    assert perms.can_view_team(world["admin"], team)
    assert not perms.can_view_team(world["rival"], team)
    assert not perms.can_view_team(world["judge"], team)
    assert not perms.can_view_team(None, team)


# --------------------------------------------------------------------------------------
# projects: editing
# --------------------------------------------------------------------------------------


def test_only_team_members_can_edit_a_project(world):
    project = make_project(world["team"])

    assert perms.can_edit_project(world["captain"], project)
    assert perms.can_edit_project(world["teammate"], project)

    for who in ("rival", "judge", "outsider", "anonymous"):
        assert not perms.can_edit_project(world[who], project), who


def test_neither_organizers_nor_admins_may_edit_a_teams_project(world):
    """A deliberate reading of the permission matrix, which lists "no" for both.

    An admin able to rewrite a submission after the deadline would undermine the single guarantee
    this software exists to provide. They get visibility and moderation instead.
    """
    project = make_project(world["team"])
    assert not perms.can_edit_project(world["organizer"], project)
    assert not perms.can_edit_project(world["admin"], project)

    # But they can see it, and they can moderate it.
    assert perms.can_view_project(world["organizer"], project)
    assert perms.can_view_project(world["admin"], project)
    assert perms.can_moderate_projects(world["organizer"], project.event)
    assert perms.can_moderate_projects(world["admin"], project.event)


# --------------------------------------------------------------------------------------
# projects: visibility
# --------------------------------------------------------------------------------------


def test_a_submitted_project_is_visible_to_everyone_including_anonymous(world):
    project = make_submitted_project(world["team"])
    for who in ("anonymous", "outsider", "judge", "rival", "organizer", "admin", "captain"):
        assert perms.can_view_project(world[who], project), who


def test_a_draft_is_visible_only_to_its_team_organizers_and_admins(world):
    draft = make_project(world["team"], status=ProjectStatus.DRAFT)

    assert perms.can_view_project(world["captain"], draft)
    assert perms.can_view_project(world["teammate"], draft)
    assert perms.can_view_project(world["organizer"], draft)
    assert perms.can_view_project(world["admin"], draft)

    # Everyone else must not even learn it exists -- these callers get a 404.
    for who in ("anonymous", "outsider", "judge", "rival", "other_organizer"):
        assert not perms.can_view_project(world[who], draft), who


def test_an_organizer_hidden_project_leaves_the_public_view(world):
    project = make_submitted_project(world["team"])
    assert perms.can_view_project(world["outsider"], project)

    project.hidden_by_organizer = True
    project.save(update_fields=["hidden_by_organizer"])

    assert not perms.can_view_project(world["outsider"], project)
    # Still visible to the team and to the organizer who hid it.
    assert perms.can_view_project(world["captain"], project)
    assert perms.can_view_project(world["organizer"], project)


def test_a_flagged_duplicate_is_not_publicly_visible(world):
    canonical = make_submitted_project(world["team"], name="Dry Harbour")
    duplicate = make_project(
        world["team"],
        status=ProjectStatus.SUBMITTED,
        name="Dry Harbour",
        duplicate_of=canonical,
    )

    assert perms.can_view_project(world["outsider"], canonical)
    assert not perms.can_view_project(world["outsider"], duplicate)
    assert perms.can_view_project(world["organizer"], duplicate)


def test_projects_in_a_non_public_event_are_not_publicly_visible(db):
    event = make_event(gallery_public=False)
    team = make_team(event)
    project = make_submitted_project(team)

    assert not perms.can_view_project(make_user(), project)
    assert not perms.can_view_project(None, project)
    assert perms.can_view_project(team.captain().user, project)


def test_scores_are_visible_to_nobody_in_t1(world):
    """T1 stores scores and shows them to no one, including organizers and admins."""
    project = make_submitted_project(world["team"])
    for who in ("anonymous", "outsider", "judge", "organizer", "admin", "captain"):
        assert perms.can_view_project_scores(world[who], project) is False, who


# --------------------------------------------------------------------------------------
# object-level and queryset-level answers must agree
# --------------------------------------------------------------------------------------


@pytest.fixture
def mixed_projects(db):
    """One of every visibility state, so the two code paths can be compared exhaustively."""
    event = make_event()
    hidden_event = make_event(gallery_public=False)

    team = make_team(event, name="Visible Team")
    other_team = make_team(event, name="Other Team")
    hidden_event_team = make_team(hidden_event, name="Quiet Team")

    canonical = make_submitted_project(team, name="Published")
    duplicate = make_project(
        team, status=ProjectStatus.SUBMITTED, name="Published", duplicate_of=canonical
    )
    draft = make_project(other_team, name="Still Drafting")
    hidden = make_submitted_project(hidden_event_team, name="Behind A Closed Gallery")

    moderated_team = make_team(event, name="Moderated Team")
    moderated = make_submitted_project(moderated_team, name="Taken Down")
    moderated.hidden_by_organizer = True
    moderated.save(update_fields=["hidden_by_organizer"])

    everything = [canonical, duplicate, draft, hidden, moderated]
    return {
        "event": event,
        "team": team,
        "all": everything,
        "publicly_visible": [canonical],
        # Assertions are scoped to these ids rather than to the whole table: the session-scoped
        # fixture import (see conftest) means ~40 unrelated organizer projects are also present,
        # and a test that asserts on global table contents would pass alone and fail in the suite.
        "own_ids": {p.pk for p in everything},
    }


def test_the_gallery_queryset_matches_the_object_level_public_check(mixed_projects):
    """`gallery_visible()` and `_is_publicly_visible` must agree on every row.

    If they drift, a project is filtered out of the listing but still readable at its own URL --
    which is precisely the leak the four conditions exist to prevent.
    """
    visible_ids = set(Project.objects.gallery_visible().values_list("pk", flat=True))

    for project in mixed_projects["all"]:
        object_level = perms.can_view_project(None, project)
        queryset_level = project.pk in visible_ids
        assert object_level == queryset_level, f"{project.name}: {object_level} vs {queryset_level}"

    assert visible_ids & mixed_projects["own_ids"] == {
        p.pk for p in mixed_projects["publicly_visible"]
    }


def test_the_two_visibility_checks_agree_across_the_whole_fixture_set(imported):
    """The same agreement, over the organizers' real 41-project dataset.

    Includes the planted duplicate (prj_41), which is the one row where the object-level and
    queryset-level answers are most likely to diverge.
    """
    visible_ids = set(Project.objects.gallery_visible().values_list("pk", flat=True))

    for project in Project.objects.select_related("event").all():
        assert perms.can_view_project(None, project) == (project.pk in visible_ids), project.name

    assert Project.objects.get(external_id="prj_41").pk not in visible_ids
    assert Project.objects.get(external_id="prj_07").pk in visible_ids


def test_visible_projects_for_an_anonymous_caller_is_exactly_the_public_gallery(mixed_projects):
    visible = set(perms.visible_projects(None).values_list("pk", flat=True))
    assert visible & mixed_projects["own_ids"] == {
        p.pk for p in mixed_projects["publicly_visible"]
    }
    # And it is the same set the gallery scoper returns, for every row in the table.
    assert visible == set(perms.gallery_projects().values_list("pk", flat=True))


def test_visible_projects_adds_a_participants_own_team(mixed_projects):
    team = mixed_projects["team"]
    captain = team.captain().user

    visible = set(perms.visible_projects(captain).values_list("pk", flat=True))
    # Their own duplicate row is theirs to see, as is the public one.
    assert {p.pk for p in mixed_projects["all"] if p.team_id == team.pk} <= visible
    # But not another team's draft.
    drafts_elsewhere = [
        p for p in mixed_projects["all"] if p.status == ProjectStatus.DRAFT and p.team_id != team.pk
    ]
    for project in drafts_elsewhere:
        assert project.pk not in visible


def test_visible_projects_gives_an_organizer_everything_in_their_own_event(mixed_projects):
    event = mixed_projects["event"]
    organizer = make_organizer(event)

    visible = set(perms.visible_projects(organizer).values_list("pk", flat=True))
    in_event = {p.pk for p in mixed_projects["all"] if p.event_id == event.pk}
    assert in_event <= visible

    # Not the other event's project, whose gallery is switched off.
    elsewhere = [p for p in mixed_projects["all"] if p.event_id != event.pk]
    for project in elsewhere:
        assert project.pk not in visible


def test_visible_projects_gives_an_admin_everything(mixed_projects):
    admin = make_admin()
    visible = set(perms.visible_projects(admin).values_list("pk", flat=True))
    assert {p.pk for p in mixed_projects["all"]} <= visible


def test_visible_projects_never_returns_duplicate_rows(mixed_projects):
    """The OR in the scoper can match one row through several branches.

    Without `distinct()` an organizer who is also a team member would see their project twice in
    the dashboard, and any `.count()` on the queryset would be wrong.
    """
    event = mixed_projects["event"]
    organizer = make_organizer(event)
    # Give them a second organizer membership elsewhere, so more branches can match.
    make_membership(organizer, make_event(), Role.ORGANIZER)

    ids = list(perms.visible_projects(organizer).values_list("pk", flat=True))
    assert len(ids) == len(set(ids))


def test_the_gallery_shows_a_team_the_same_thing_it_shows_a_stranger(mixed_projects):
    """Otherwise a team sees their own draft in the listing, assumes it is public, and never
    submits it."""
    team = mixed_projects["team"]
    captain = team.captain().user

    gallery = set(perms.gallery_projects().values_list("pk", flat=True))
    assert gallery & mixed_projects["own_ids"] == {
        p.pk for p in mixed_projects["publicly_visible"]
    }

    # The captain's own view is strictly larger -- it includes their draft and duplicate -- which
    # is exactly why the gallery must not be built from it.
    own_view = set(perms.visible_projects(captain).values_list("pk", flat=True))
    assert own_view > gallery
    assert (own_view - gallery) & mixed_projects["own_ids"]


# --------------------------------------------------------------------------------------
# event and team scopers
# --------------------------------------------------------------------------------------


def test_manageable_events_is_empty_for_everyone_but_organizers_and_admins(world):
    assert list(perms.manageable_events(None)) == []
    assert list(perms.manageable_events(world["outsider"])) == []
    assert list(perms.manageable_events(world["judge"])) == []
    assert list(perms.manageable_events(world["captain"])) == []

    assert [e.pk for e in perms.manageable_events(world["organizer"])] == [world["event"].pk]
    assert perms.manageable_events(world["admin"]).count() >= 2


def test_a_non_public_event_is_still_listed_for_its_own_members(db):
    """Hiding an event from its own organizer would be absurd."""
    event = make_event(gallery_public=False)
    organizer = make_organizer(event)
    team = make_team(event)

    assert event in perms.visible_events(organizer)
    assert event in perms.visible_events(team.captain().user)
    assert event not in perms.visible_events(make_user())
    assert event not in perms.visible_events(None)
