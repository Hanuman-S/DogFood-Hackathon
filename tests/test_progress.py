"""The organizer's live judging-progress dashboard."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership
from organizer.progress import progress_data
from projects.models import Project, Status
from scoring.models import Assignment, AssignmentSource, Score

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(make_event, make_team, make_user):
    """Three judges and three projects: one judge done, one part-way, one not started."""
    event = make_event()
    projects = [
        Project.objects.create(team=make_team(event), name=f"P{i}", status=Status.SUBMITTED, submitted_at=timezone.now())
        for i in range(3)
    ]
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=3, hours=1), submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - timedelta(days=1), judging_starts_at=now - timedelta(hours=12),
        judging_ends_at=now + timedelta(days=2),
    )
    event.refresh_from_db()
    judges = {name: EventMembership.objects.create(event=event, user=make_user(name=name), role=Role.JUDGE)
              for name in ("Done", "Halfway", "Idle")}
    for judge in judges.values():
        for project in projects[:2]:
            Assignment.objects.create(judge=judge, project=project, source=AssignmentSource.MANUAL)
    for project in projects[:2]:
        Score.objects.create(judge=judges["Done"], project=project, submitted_at=timezone.now())
    Score.objects.create(judge=judges["Halfway"], project=projects[0], submitted_at=timezone.now())
    Score.objects.create(judge=judges["Halfway"], project=projects[1])  # a draft
    return event, judges, projects


def test_each_judges_state_and_counts(world):
    event, judges, _ = world
    rows = {r["judge"].pk: r for r in progress_data(event)["judge_rows"]}
    assert rows[judges["Done"].pk]["state"] == "done"
    assert (rows[judges["Halfway"].pk]["state"], rows[judges["Halfway"].pk]["submitted"], rows[judges["Halfway"].pk]["drafts"]) == ("in progress", 1, 1)
    assert (rows[judges["Idle"].pk]["state"], rows[judges["Idle"].pk]["unstarted"]) == ("not started", 2)
    # the ones to chase come first
    assert progress_data(event)["judge_rows"][0]["judge"].pk == judges["Idle"].pk


def test_project_and_overall_counts_use_submitted_reviews_only(world):
    event, _, projects = world
    data = progress_data(event)
    in_by_project = {r["project"].pk: r["in"] for r in data["project_rows"]}
    assert in_by_project == {projects[0].pk: 2, projects[1].pk: 1, projects[2].pk: 0}
    assert (data["total_in"], data["total_needed"]) == (3, 9)  # target 3, drafts not counted
    assert data["not_started"] == 1 and data["short_projects"] == 3


def test_only_this_events_organizers_see_progress(world, client_for, make_user):
    event, judges, _ = world
    url = f"/organizer/events/{event.slug}/progress"
    page = client_for(event.organizer).get(url)
    assert page.status_code == 200 and b"not started" in page.content and b"data-refresh-url" in page.content
    assert client_for(make_user(role=Role.ORGANIZER)).get(url).status_code == 404
    assert client_for(judges["Done"].user).get(url).status_code == 403


def test_the_refresh_returns_just_the_tables(world, client_for):
    event, _, _ = world
    partial = client_for(event.organizer).get(f"/organizer/events/{event.slug}/progress?partial=1")
    assert partial.status_code == 200
    assert b"<html" not in partial.content and b"judges-progress" in partial.content


def test_a_nudge_is_logged_and_written_out_for_the_organizers_mail(world, client_for):
    event, judges, _ = world
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/progress/nudge/{judges['Idle'].pk}")
    assert response.status_code == 200
    body = response.content.decode()
    assert f"mailto:{judges['Idle'].user.email.replace('@', '%40')}" in body and "Reminder" in body
    entry = AuditLog.objects.get(action=AuditAction.JUDGE_NUDGED)
    assert entry.detail["email"] == judges["Idle"].user.email and entry.actor == event.organizer
    page = client.get(f"/organizer/events/{event.slug}/progress").content.decode()
    assert "nudged" not in page  # the mail goes from the organizer's own inbox, so we never claim it went


def test_a_judge_of_another_event_cannot_be_nudged_from_here(world, client_for, make_event, make_user):
    event, _, _ = world
    elsewhere = EventMembership.objects.create(event=make_event(), user=make_user(), role=Role.JUDGE)
    response = client_for(event.organizer).post(f"/organizer/events/{event.slug}/progress/nudge/{elsewhere.pk}")
    assert response.status_code == 404
    assert not AuditLog.objects.filter(action=AuditAction.JUDGE_NUDGED).exists()


def test_the_event_page_links_to_assignment_and_progress(world, client_for):
    event, _, _ = world
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/").content.decode()
    assert f"/organizer/events/{event.slug}/assignments" in page and f"/organizer/events/{event.slug}/progress" in page
