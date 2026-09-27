"""Importing the organizers' fixture file."""

import json
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

from accounts.models import User
from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership, JudgeTrack
from imports.fixtures import FixtureError, Importer, import_file, load
from imports.models import FixtureRef
from projects.models import Project, Status
from scoring.models import Criterion, Score, ScoreItem
from teams.models import Team, TeamMember

pytestmark = pytest.mark.django_db

FIXTURES = Path(settings.FIXTURES_PATH)
needs_file = pytest.mark.skipif(not FIXTURES.exists(), reason="acceptance/fixtures.json not present")


@needs_file
def test_the_real_fixture_file_imports_with_its_edge_cases_reported():
    report = import_file(FIXTURES)
    data = json.loads(FIXTURES.read_text())
    event = Event.objects.get()
    assert event.name == data["event"]["name"] and event.is_published
    assert event.tracks.count() == len(data["tracks"])
    assert Team.objects.filter(event=event).count() == len(data["teams"])
    # Judges are judges *of this event*, with the tracks the file lists for them.
    judges = EventMembership.objects.filter(event=event, role=Role.JUDGE)
    assert judges.count() == len(data["judges"])
    assert JudgeTrack.objects.filter(membership__in=judges).count() == sum(len(j["tracks"]) for j in data["judges"])
    # Every team member is a participant of the event, and nobody is on both sides.
    members = {m for t in data["teams"] for m in t["members"]}
    assert EventMembership.objects.filter(event=event, role=Role.PARTICIPANT).count() == len(members)
    # 41 rows in the file, one of them a second submission by the same team
    assert Project.objects.filter(event=event, status=Status.SUBMITTED).count() == len(data["projects"]) - 1
    assert "prj_41" in report.duplicates[0] and "second submission" in report.duplicates[0]
    dup = FixtureRef.objects.get(kind="project", external_id="prj_41")
    assert dup.duplicate_of == "prj_07"
    assert dup.object_id == FixtureRef.objects.get(kind="project", external_id="prj_07").object_id
    assert AuditLog.objects.filter(action=AuditAction.FIXTURES_IMPORTED).exists()


@needs_file
def test_the_fixture_scores_are_imported_against_judge_memberships():
    report = import_file(FIXTURES)
    data = json.loads(FIXTURES.read_text())
    event = Event.objects.get()
    keys = {k for s in data["scores"] for k in s["criteria"]}
    assert set(Criterion.objects.filter(event=event).values_list("key", flat=True)) == keys
    assert all(c.weight == 1 for c in Criterion.objects.all())

    # prj_41 is folded into prj_07, so a judge who reviewed both would have two reviews of one
    # project. The review of the kept submission wins; the other is reported, never dropped silently.
    by_judge = {}
    for s in data["scores"]:
        by_judge.setdefault(s["judge"], set()).add(s["project"])
    collisions = [j for j, projects in by_judge.items() if {"prj_07", "prj_41"} <= projects]
    assert Score.objects.count() == len(data["scores"]) - len(collisions)
    assert ScoreItem.objects.count() == Score.objects.count() * len(keys)
    review_dups = [line for line in report.duplicates if line.startswith("review by")]
    assert len(review_dups) == len(collisions)
    assert all(s.judge.role == Role.JUDGE and s.judge.event_id == event.pk for s in Score.objects.select_related("judge"))

    # Idempotent: a second run creates no scores.
    again = import_file(FIXTURES)
    assert again.created.get("score", 0) == 0
    assert Score.objects.count() == len(data["scores"]) - len(collisions)


@needs_file
def test_import_is_idempotent_and_create_only():
    import_file(FIXTURES)
    project = Project.objects.first()
    Project.objects.filter(pk=project.pk)  # an organizer edit, made through the bypass
    from core.deadlines import deadline_bypass

    with deadline_bypass(None, "test edit"):
        Project.objects.filter(pk=project.pk).update(tagline="edited by an organizer")
    counts = (Event.objects.count(), Team.objects.count(), Project.objects.count(), User.objects.count())
    report = import_file(FIXTURES)
    assert sum(report.created.values()) == 0
    assert (Event.objects.count(), Team.objects.count(), Project.objects.count(), User.objects.count()) == counts
    assert Project.objects.get(pk=project.pk).tagline == "edited by an organizer"


@needs_file
def test_fixture_titles_are_on_gallery_page_one():
    import_file(FIXTURES)
    data = json.loads(FIXTURES.read_text())
    body = Client().get("/projects").content.decode()
    for title in [p["title"] for p in data["projects"][:3]]:
        assert title in body


@needs_file
def test_imported_projects_are_past_the_deadline_and_frozen():
    import_file(FIXTURES)
    team = Team.objects.first()
    user = team.captain
    user.set_password("pw-for-this-test-123")
    user.save()
    client = Client()
    client.post("/login", {"email": user.email, "password": "pw-for-this-test-123"})
    response = client.post(f"/participant/projects/{team.project.pk}/", {"name": "late"})
    assert response.status_code == 409


def small(**overrides):
    data = {
        "event": {"id": "evt_x", "name": "Tiny Hack", "submissions_close": "2026-01-10T18:00:00Z"},
        "tracks": [{"id": "trk_x", "name": "Only"}],
        "judges": [{"id": "jdg_x", "name": "Jo Judge", "email": "jo@example.org", "tracks": ["trk_x"]}],
        "teams": [
            {"id": "tm_a", "name": "Alpha", "members": ["a1@example.org", "jo@example.org"]},
            {"id": "tm_b", "name": "Beta", "members": ["a1@example.org", "b1@example.org"]},
        ],
        "projects": [
            {"id": "p1", "team": "tm_a", "track": "trk_x", "title": "One", "summary": "s",
             "repo_url": "https://example.org/1", "submitted_at": "2026-01-10T10:00:00Z"},
            {"id": "p2", "team": "tm_zz", "track": "trk_x", "title": "Orphan", "summary": "s",
             "repo_url": "https://example.org/2", "submitted_at": "2026-01-10T11:00:00Z"},
        ],
        "scores": [],
    }
    data.update(overrides)
    return data


def test_conflicts_are_reported_not_silently_fixed():
    report = Importer(small()).run()
    joined = " ".join(report.conflicts)
    assert "jo@example.org is staff in this event" in joined  # judge listed as a team member
    assert "a1@example.org is on two teams" in joined          # one person, two teams
    assert "p2 belongs to unknown team" in joined
    assert not TeamMember.objects.filter(user__email="jo@example.org").exists()
    jo = User.objects.get(email="jo@example.org")
    assert set(jo.event_memberships.values_list("role", flat=True)) == {Role.JUDGE}


@override_settings(DEMO_MODE=False)
def test_imported_accounts_cannot_log_in_outside_demo_mode():
    Importer(small()).run()
    assert not User.objects.get(email="a1@example.org").has_usable_password()


def test_a_bad_file_is_refused_whole(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{not json")
    with pytest.raises(FixtureError):
        load(path)
    path.write_text(json.dumps({"event": {}}))
    with pytest.raises(FixtureError):
        load(path)
    with pytest.raises(FixtureError):
        load(tmp_path / "missing.json")
    assert not Event.objects.exists()
