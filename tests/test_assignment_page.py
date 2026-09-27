"""The assignment page says plainly what each project needs and what can be done about it."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.roles import Role
from events.models import Event, EventMembership
from projects.models import Project, Status
from scoring import services
from scoring.models import Assignment, AssignmentStatus, Score

pytestmark = pytest.mark.django_db


class FakeRequest:
    def __init__(self, user):
        self.user, self.META, self.headers = user, {"REMOTE_ADDR": "10.0.0.1"}, {}


@pytest.fixture
def two_judge_event(make_event, make_team, make_user):
    """The Archive situation: two judges, target 2, so every project has both of them."""
    event = make_event()
    projects = [Project.objects.create(team=make_team(event), name=f"P{i}", status=Status.SUBMITTED,
                                       submitted_at=timezone.now()) for i in range(2)]
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=3, hours=1), submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - timedelta(hours=2), judging_starts_at=now - timedelta(hours=1),
        judging_ends_at=now + timedelta(days=3))
    event.refresh_from_db()
    judges = [EventMembership.objects.create(event=event, user=make_user(name=n), role=Role.JUDGE) for n in ("Ann", "Ben")]
    services.run_assignment(FakeRequest(event.organizer), event, target=2)
    return event, judges, projects


def page(client, event):
    return client.get(f"/organizer/events/{event.slug}/assignments").content.decode()


def test_counts_say_reviews_in_against_the_target(two_judge_event, client_for):
    event, judges, projects = two_judge_event
    Score.objects.create(judge=judges[0], project=projects[0], submitted_at=timezone.now())
    html = page(client_for(event.organizer), event)
    assert "1 of 2 reviews in" in html and "0 of 2 reviews in" in html


def test_no_empty_move_box_when_nobody_can_take_the_review(two_judge_event, client_for):
    event, _, _ = two_judge_event
    html = page(client_for(event.organizer), event)
    assert 'name="judge"' not in html  # no move-to or add dropdown with nothing in it
    assert "no other judge can take this project" in html and "withdraw" in html


def test_a_declined_project_with_no_judge_left_says_how_to_fix_it(two_judge_event, client_for):
    event, judges, projects = two_judge_event
    assignment = Assignment.objects.get(judge=judges[0], project=projects[0])
    services.decline_assignment(FakeRequest(judges[0].user), assignment, "my friend's team")
    html = page(client_for(event.organizer), event)
    assert "needs 1 more" in html and "no judge left" in html
    assert f"/organizer/events/{event.slug}/#judges" in html and "add or invite" in html


def test_with_a_third_judge_the_gap_can_be_filled_and_moves_work(two_judge_event, client_for, make_user):
    event, judges, projects = two_judge_event
    assignment = Assignment.objects.get(judge=judges[0], project=projects[0])
    services.decline_assignment(FakeRequest(judges[0].user), assignment, "conflict")
    cara = EventMembership.objects.create(event=event, user=make_user(name="Cara"), role=Role.JUDGE)
    client = client_for(event.organizer)
    html = page(client, event)
    assert "press assign" in html and "Cara" in html  # Cara is offered in add / move to
    client.post(f"/organizer/events/{event.slug}/assignments", {"target": "2", "max_load": "", "seed": ""})
    assert Assignment.objects.filter(judge=cara, project=projects[0], status=AssignmentStatus.ASSIGNED).exists()
    # move Ben's unstarted review of P1 to Cara
    ben_p1 = Assignment.objects.get(judge=judges[1], project=projects[1], status=AssignmentStatus.ASSIGNED)
    client.post(f"/organizer/events/{event.slug}/assignments/{ben_p1.pk}/move", {"judge": cara.pk})
    assert Assignment.objects.filter(judge=cara, project=projects[1], status=AssignmentStatus.ASSIGNED).exists()
    assert "reassigned" in page(client, event)


def test_the_board_refreshes_from_a_partial_with_the_pages_gates(two_judge_event, client_for, make_user):
    event, judges, _ = two_judge_event
    client = client_for(event.organizer)
    assert "data-refresh-url" in page(client, event)
    partial = client.get(f"/organizer/events/{event.slug}/assignments?partial=1")
    html = partial.content.decode()
    assert partial.status_code == 200 and "<html" not in html
    assert "0 of 2 reviews in" in html and "where it stands" in html
    assert 'name="target"' not in html  # the run form is not refreshed under the organizer's hands
    assert client_for(make_user(role=Role.ORGANIZER)).get(
        f"/organizer/events/{event.slug}/assignments?partial=1").status_code == 404
    assert client_for(judges[0].user).get(
        f"/organizer/events/{event.slug}/assignments?partial=1").status_code == 403
