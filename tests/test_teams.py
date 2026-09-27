"""Teams: creation, invite links, captaincy, leaving, and the one-team-per-event rule."""

import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from projects.models import Project, Status
from teams.models import Team, TeamMember

pytestmark = pytest.mark.django_db


def create(client, event, name="Nightshift"):
    return client.post(f"/participant/events/{event.slug}/team", {"name": name})


def test_create_team_makes_you_captain(make_event, make_user, client_for):
    event = make_event()
    user = make_user()
    create(client_for(user), event)
    team = Team.objects.get(event=event)
    assert team.captain == user
    assert list(team.members.values_list("user", flat=True)) == [user.pk]
    assert AuditLog.objects.filter(action=AuditAction.TEAM_CREATED).exists()


def test_one_team_per_participant_per_event(make_event, make_user, client_for):
    event = make_event()
    client = client_for(make_user())
    create(client, event, "First")
    create(client, event, "Second")
    assert Team.objects.filter(event=event).count() == 1
    create(client, make_event(), "Elsewhere")  # a different event is fine
    assert Team.objects.count() == 2


def test_the_database_refuses_a_second_team_in_one_event(make_event, make_team, make_user):
    event = make_event()
    user = make_user()
    make_team(event, captain=user)
    other = make_team(event)
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamMember.objects.create(team=other, user=user)


def test_team_names_are_unique_per_event_in_any_case(make_event, make_team, make_user, client_for):
    event = make_event()
    make_team(event, name="Nightshift")
    create(client_for(make_user()), event, "NIGHTSHIFT")
    assert Team.objects.filter(event=event).count() == 1


@pytest.mark.parametrize("role", [Role.JUDGE, Role.ORGANIZER])
def test_staff_of_the_event_cannot_be_on_its_teams(make_event, make_team, make_user, client_for, role):
    from events.models import EventMembership

    event = make_event()
    team = make_team(event)
    person = make_user()
    EventMembership.objects.create(user=person, event=event, role=role)
    staff = client_for(person)
    assert staff.post(f"/join/{team.invite_token}").status_code == 302
    assert not TeamMember.objects.filter(user=staff.user).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_JOIN_REFUSED).exists()


def test_unpublished_event_takes_no_teams(make_event, make_user, client_for):
    event = make_event(published=False)
    assert create(client_for(make_user()), event).status_code == 404
    assert not Team.objects.exists()


# --- invite links --------------------------------------------------------------------------------


def test_join_by_link(make_event, make_team, make_user, client_for):
    event = make_event()
    team = make_team(event)
    joiner = client_for(make_user())
    response = joiner.post(f"/join/{team.invite_token}")
    assert response.status_code == 302
    assert team.members.filter(user=joiner.user).exists()


def test_visitor_sees_the_invite_and_is_asked_to_log_in(make_event, make_team):
    team = make_team(make_event())
    response = Client().get(f"/join/{team.invite_token}")
    assert response.status_code == 200
    assert team.name.encode() in response.content and b"log in" in response.content
    post = Client().post(f"/join/{team.invite_token}")
    assert post["Location"] == f"/login?next=/join/{team.invite_token}"


def test_unknown_link_is_404():
    assert Client().get("/join/nope").status_code == 404


def test_full_team_refuses_joiners(make_event, make_team, make_user, client_for):
    event = make_event(max_team_size=2)
    team = make_team(event, members=[make_user()])
    late = client_for(make_user())
    late.post(f"/join/{team.invite_token}")
    assert team.members.count() == 2
    assert not team.members.filter(user=late.user).exists()


def test_already_on_a_team_cannot_join_another(make_event, make_team, make_user, client_for):
    event = make_event()
    user = make_user()
    make_team(event, captain=user)
    other = make_team(event)
    client_for(user).post(f"/join/{other.invite_token}")
    assert not other.members.filter(user=user).exists()


def test_replacing_the_link_kills_the_old_one(make_event, make_team, make_user, client_for):
    event = make_event()
    team = make_team(event)
    old = team.invite_token
    client_for(team.captain).post(f"/participant/teams/{team.pk}/reset-link")
    team.refresh_from_db()
    assert team.invite_token != old
    assert Client().get(f"/join/{old}").status_code == 404


# --- captaincy -----------------------------------------------------------------------------------


def test_only_the_captain_manages_the_team(make_event, make_team, make_user, client_for):
    event = make_event()
    member = make_user()
    team = make_team(event, members=[member])
    client = client_for(member)
    client.post(f"/participant/teams/{team.pk}/rename", {"name": "Hijacked"})
    client.post(f"/participant/teams/{team.pk}/reset-link")
    captain_row = team.members.get(user=team.captain)
    client.post(f"/participant/teams/{team.pk}/members/{captain_row.pk}/remove")
    team.refresh_from_db()
    assert team.name != "Hijacked"
    assert team.members.count() == 2


def test_non_members_get_404_on_team_actions(make_event, make_team, make_user, client_for):
    team = make_team(make_event())
    assert client_for(make_user()).post(f"/participant/teams/{team.pk}/leave").status_code == 404


def test_captain_renames_removes_and_hands_over(make_event, make_team, make_user, client_for):
    event = make_event()
    a, b = make_user(), make_user()
    team = make_team(event, members=[a, b])
    captain = client_for(team.captain)
    captain.post(f"/participant/teams/{team.pk}/rename", {"name": "Renamed"})
    captain.post(f"/participant/teams/{team.pk}/members/{team.members.get(user=a).pk}/remove")
    captain.post(f"/participant/teams/{team.pk}/members/{team.members.get(user=b).pk}/captain")
    team.refresh_from_db()
    assert team.name == "Renamed"
    assert not team.members.filter(user=a).exists()
    assert team.captain == b


def test_captain_must_hand_over_before_leaving(make_event, make_team, make_user, client_for):
    event = make_event()
    team = make_team(event, members=[make_user()])
    client_for(team.captain).post(f"/participant/teams/{team.pk}/leave")
    assert team.members.filter(user=team.captain).exists()


def test_member_can_leave(make_event, make_team, make_user, client_for):
    event = make_event()
    member = make_user()
    team = make_team(event, members=[member])
    client_for(member).post(f"/participant/teams/{team.pk}/leave")
    assert not team.members.filter(user=member).exists()


def test_last_member_leaving_disbands_the_team_and_its_draft(make_event, make_team, client_for):
    event = make_event()
    team = make_team(event)
    Project.objects.create(team=team, name="Draft")
    client_for(team.captain).post(f"/participant/teams/{team.pk}/leave")
    assert not Team.objects.filter(pk=team.pk).exists()
    assert not Project.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_DISBANDED).exists()


def test_last_member_cannot_disband_a_submitted_project(make_event, make_team, client_for):
    from django.utils import timezone

    event = make_event()
    team = make_team(event)
    Project.objects.create(team=team, name="Done", status=Status.SUBMITTED, submitted_at=timezone.now())
    client_for(team.captain).post(f"/participant/teams/{team.pk}/leave")
    assert Team.objects.filter(pk=team.pk).exists()
