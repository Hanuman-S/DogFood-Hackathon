"""Deadline enforcement: the service check, the database trigger, extensions, and responses."""

import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import ApiToken
from accounts.roles import Role
from core.deadlines import deadline_bypass, is_closed
from core.models import AuditAction, AuditLog
from events.models import Event
from projects.models import Answer, Project, ProjectImage, Status, Tag
from teams.models import Team, TeamExtension, TeamMember

from _dates import dt_fields

pytestmark = pytest.mark.django_db


def close(event, ago=timedelta(hours=1)):
    """Move the event's close into the past (events are not trigger-guarded)."""
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=3, hours=1), submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - ago,
    )
    event.refresh_from_db()
    return event


@pytest.fixture
def world(make_event, make_team, make_user, client_for):
    event = make_event()
    member = make_user()
    team = make_team(event, members=[member])
    project = Project.objects.create(
        team=team, name="Quiet Hours", tagline="t", description="d", repo_url="https://x.org/r"
    )
    return {"event": event, "team": team, "project": project, "member": member,
            "captain": client_for(team.captain), "client_for": client_for, "make_user": make_user}


def assert_closed(response):
    assert response.status_code == 409, response.content[:300]
    if response["Content-Type"].startswith("application/json"):
        assert response.json()["error"] == "submissions_closed"
    else:
        assert b"submissions closed" in response.content


# --- the rule -------------------------------------------------------------------------------------


def test_the_close_instant_itself_is_closed():
    t = timezone.now()
    assert is_closed(t, t)
    assert not is_closed(t - timedelta(microseconds=1), t)


def test_every_participant_write_is_refused_after_the_close(world):
    event, team, project, captain = world["event"], world["team"], world["project"], world["captain"]
    close(event)
    p = project.pk
    before = (project.name, project.status, team.name, team.invite_token, team.members.count())
    assert_closed(captain.post(f"/participant/projects/{p}/", {"name": "Late edit"}))
    assert_closed(captain.post(f"/participant/projects/{p}/submit"))
    assert_closed(captain.post(f"/participant/projects/{p}/images", {"image": SimpleUploadedFile("a.png", b"x")}))
    assert_closed(captain.post(f"/participant/teams/{team.pk}/rename", {"name": "Late name"}))
    assert_closed(captain.post(f"/participant/teams/{team.pk}/reset-link"))
    row = team.members.get(user=world["member"])
    assert_closed(captain.post(f"/participant/teams/{team.pk}/members/{row.pk}/remove"))
    assert_closed(world["client_for"](world["member"]).post(f"/participant/teams/{team.pk}/leave"))
    newcomer = world["client_for"](world["make_user"]())
    assert_closed(newcomer.post(f"/join/{team.invite_token}"))
    assert_closed(newcomer.post(f"/participant/events/{event.slug}/team", {"name": "Late team"}))
    assert_closed(newcomer.post(f"/participant/events/{event.slug}/project", {"name": "Late project"}))
    project.refresh_from_db()
    team.refresh_from_db()
    assert (project.name, project.status, team.name, team.invite_token, team.members.count()) == before
    assert Team.objects.count() == 1 and Project.objects.count() == 1


def test_a_submitted_project_cannot_be_withdrawn_after_the_close(world):
    project = world["project"]
    project.status, project.submitted_at = Status.SUBMITTED, timezone.now()
    project.save()
    close(world["event"])
    assert_closed(world["captain"].post(f"/participant/projects/{project.pk}/unsubmit"))
    project.refresh_from_db()
    assert project.status == Status.SUBMITTED


def test_a_late_invalid_edit_is_refused_as_late_not_as_invalid(world):
    close(world["event"])
    response = world["captain"].post(f"/participant/projects/{world['project'].pk}/", {"repo_url": "javascript:x"})
    assert response.status_code == 409


def test_refusals_are_audited_with_how_late_they_were(world):
    close(world["event"], ago=timedelta(minutes=10))
    world["captain"].post(f"/participant/projects/{world['project'].pk}/submit")
    row = AuditLog.objects.filter(action=AuditAction.LATE_WRITE_REFUSED).latest("created_at")
    assert row.subject == world["event"].slug
    assert 590 <= row.detail["late_by_seconds"] <= 700
    assert row.detail["attempted"] == "submit the project"


def test_the_api_answers_409_with_the_close_time(world):
    close(world["event"])
    _, raw = ApiToken.issue(world["team"].captain, "t")
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    response = Client().patch(
        f"/api/projects/{world['project'].pk}", json.dumps({"name": "late"}), content_type="application/json", **auth
    )
    assert_closed(response)
    body = response.json()
    assert body["closed_at"].endswith("Z") and body["late_by_seconds"] >= 3500
    create = Client().post(f"/api/events/{world['event'].slug}/projects", json.dumps({"name": "x"}),
                           content_type="application/json", **auth)
    assert_closed(create)


def test_pages_turn_read_only_after_the_close(world):
    close(world["event"])
    page = world["captain"].get(f"/participant/projects/{world['project'].pk}/")
    assert page.status_code == 200
    assert b'class="frozen stack" disabled' in page.content
    assert b"submissions closed at" in page.content


# --- before submissions open ----------------------------------------------------------------------


def test_teams_can_form_before_submissions_open_but_projects_cannot_start(make_event, make_user, client_for):
    now = timezone.now()
    event = make_event(submissions_open_at=now + timedelta(days=1),
                       submissions_close_at=now + timedelta(days=3), judging_ends_at=now + timedelta(days=5))
    client = client_for(make_user())
    client.post(f"/participant/events/{event.slug}/team", {"name": "Early birds"})
    assert Team.objects.filter(event=event).exists()
    response = client.post(f"/participant/events/{event.slug}/project", {"name": "Too soon"})
    assert response.status_code == 409 and b"not open yet" in response.content
    assert not Project.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.EARLY_WRITE_REFUSED).exists()


# --- the database trigger ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "write",
    [
        lambda w: Project.objects.filter(pk=w["project"].pk).update(name="raw sql"),
        lambda w: Answer.objects.create(project=w["project"], question=w["question"], value="late"),
        lambda w: w["project"].tags.add(Tag.objects.create(name="late")),
        lambda w: TeamMember.objects.create(team=w["team"], user=w["make_user"]()),
        lambda w: Team.objects.filter(pk=w["team"].pk).update(name="raw rename"),
        lambda w: TeamMember.objects.filter(team=w["team"], user=w["member"]).delete(),
        lambda w: Project.objects.filter(pk=w["project"].pk).delete(),
    ],
    ids=["project-update", "answer-insert", "tag-insert", "member-insert", "team-update", "member-delete", "project-delete"],
)
def test_the_database_refuses_late_writes_that_skip_the_service_layer(world, write):
    from events.models import CustomQuestion

    world["question"] = CustomQuestion.objects.create(event=world["event"], prompt="Q")
    close(world["event"])
    with pytest.raises(DatabaseError, match="dogfood_submissions_closed"), transaction.atomic():
        write(world)
    assert Project.objects.filter(pk=world["project"].pk, name="Quiet Hours").exists()


def test_the_trigger_refusal_becomes_a_409_even_if_a_view_forgets_the_check(world, monkeypatch):
    """Simulate a code path that forgot the service check: the database still says no, and the
    client still gets a clean 409 rather than a 500."""
    import projects.services as project_services

    monkeypatch.setattr(project_services, "check_submission_window", lambda *a, **k: None)
    import participant.views as views

    monkeypatch.setattr(views.deadlines, "check_submission_window", lambda *a, **k: None)
    close(world["event"])
    response = world["captain"].post(f"/participant/projects/{world['project'].pk}/", {"name": "Sneaky"})
    assert response.status_code == 409
    world["project"].refresh_from_db()
    assert world["project"].name == "Quiet Hours"


def test_an_organizer_bypass_can_write_after_the_close_and_is_audited(world):
    close(world["event"])
    with deadline_bypass(None, "judging fix"):
        Project.objects.filter(pk=world["project"].pk).update(name="Fixed by organizer")
    assert Project.objects.get(pk=world["project"].pk).name == "Fixed by organizer"
    # ...and the bypass ends with its transaction
    with pytest.raises(DatabaseError), transaction.atomic():
        Project.objects.filter(pk=world["project"].pk).update(name="Leaked bypass")


# --- extensions -------------------------------------------------------------------------------------


def test_a_team_extension_reopens_that_team_only(world, make_team, client_for):
    other = make_team(world["event"])  # formed while still open
    event = close(world["event"])
    organizer = client_for(event.organizer)
    until = timezone.now() + timedelta(hours=2)
    response = organizer.post(f"/organizer/events/{event.slug}/deadline/teams",
                              {"team": world["team"].pk, **dt_fields("until", until), "reason": "upload failed"})
    assert response.status_code == 302
    assert world["captain"].post(f"/participant/projects/{world['project'].pk}/", {"name": "Saved late"}).status_code == 302
    assert Project.objects.get(pk=world["project"].pk).name == "Saved late"
    assert_closed(client_for(other.captain).post(f"/participant/teams/{other.pk}/rename", {"name": "Nope"}))
    assert AuditLog.objects.filter(action=AuditAction.TEAM_EXTENSION_GRANTED, detail__reason="upload failed").exists()


def test_the_trigger_honours_extensions_too(world):
    close(world["event"])
    TeamExtension.objects.create(team=world["team"], until=timezone.now() + timedelta(hours=1), reason="r")
    Project.objects.filter(pk=world["project"].pk).update(name="Raw but extended")
    assert Project.objects.get(pk=world["project"].pk).name == "Raw but extended"


def test_revoking_an_extension_closes_the_team_again(world, client_for):
    event = close(world["event"])
    extension = TeamExtension.objects.create(team=world["team"], until=timezone.now() + timedelta(hours=1), reason="r")
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/deadline/teams/{extension.pk}/revoke")
    assert_closed(world["captain"].post(f"/participant/projects/{world['project'].pk}/submit"))


@pytest.mark.parametrize("hours, ok", [(-0.5, False), (2, True), (24 * 30, False)])
def test_extension_must_end_after_the_close_and_before_judging_starts(world, client_for, hours, ok):
    event = close(world["event"])
    until = timezone.now() + timedelta(hours=hours)
    response = client_for(event.organizer).post(
        f"/organizer/events/{event.slug}/deadline/teams", {"team": world["team"].pk, **dt_fields("until", until), "reason": "r"}
    )
    assert TeamExtension.objects.exists() is ok
    assert response.status_code == (302 if ok else 400)


def test_extending_for_everyone_reopens_every_team_and_keeps_the_original(world, client_for):
    event = close(world["event"])
    original = event.submissions_close_at
    new_close = timezone.now() + timedelta(hours=3)
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/deadline/extend",
                                     {**dt_fields("new_close", new_close), "reason": "venue wifi died"})
    event.refresh_from_db()
    assert event.original_submissions_close_at == original
    assert event.submissions_close_at > timezone.now()
    assert world["captain"].post(f"/participant/projects/{world['project'].pk}/submit").status_code == 302
    assert AuditLog.objects.filter(action=AuditAction.DEADLINE_EXTENDED, detail__reason="venue wifi died").exists()


def test_extending_into_judging_moves_judging_and_results_by_the_same_amount(world, client_for):
    event = world["event"]
    Event.objects.filter(pk=event.pk).update(results_at=event.judging_ends_at + timedelta(days=1))
    event.refresh_from_db()
    before = [event.judging_starts_at, event.judging_ends_at, event.results_at]
    gap = event.judging_ends_at - event.submissions_close_at
    new_close = (event.judging_ends_at + timedelta(days=1)).replace(second=0, microsecond=0)
    shift = new_close - event.submissions_close_at
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/deadline/extend",
                                     {**dt_fields("new_close", new_close), "reason": "r"})
    event.refresh_from_db()
    assert event.submissions_close_at == new_close.replace(second=0, microsecond=0)
    assert event.judging_ends_at - event.submissions_close_at >= gap - timedelta(minutes=1)
    assert [event.judging_starts_at, event.judging_ends_at, event.results_at] == [d + shift for d in before]


def test_extension_must_move_the_close_later(world, client_for):
    event = world["event"]
    earlier = event.submissions_close_at - timedelta(hours=1)
    response = client_for(event.organizer).post(f"/organizer/events/{event.slug}/deadline/extend",
                                                {**dt_fields("new_close", earlier), "reason": "r"})
    assert response.status_code == 400


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_organizers_grant_extensions(world, role, make_user, client_for):
    event = close(world["event"])
    until = timezone.now() + timedelta(hours=2)
    client_for(make_user(role=role)).post(f"/organizer/events/{event.slug}/deadline/teams",
                                          {"team": world["team"].pk, **dt_fields("until", until), "reason": "r"})
    assert not TeamExtension.objects.exists()


def test_another_events_organizer_cannot_extend(world, make_user, client_for):
    event = close(world["event"])
    until = timezone.now() + timedelta(hours=2)
    response = client_for(make_user(role=Role.ORGANIZER)).post(
        f"/organizer/events/{event.slug}/deadline/teams", {"team": world["team"].pk, **dt_fields("until", until), "reason": "r"}
    )
    assert response.status_code == 404 and not TeamExtension.objects.exists()


# --- the checker's route --------------------------------------------------------------------------


def test_seeded_closed_event_refuses_the_checkers_post(settings):
    from io import StringIO

    from django.core.management import call_command

    settings.DEMO_TOKENS = {"participant": "demo-participant-token"}
    call_command("seed_demo", stdout=StringIO())
    response = Client().post("/api/events/dogfood-archive-2026/projects", json.dumps({"name": "x"}),
                             content_type="application/json", HTTP_AUTHORIZATION="Bearer demo-participant-token")
    assert_closed(response)
    assert Project.objects.filter(event__slug="dogfood-archive-2026", status=Status.SUBMITTED).count() == 1
