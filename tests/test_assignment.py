"""Assigning judges: the hard rules, balance, seeded randomness, connectivity, manual changes,
declines and stalled judges -- and the organizers' fixture data topped up to three reviews."""

from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership, JudgeTrack, Track
from imports.fixtures import import_file
from projects.models import Project, Status
from scoring import services
from scoring.assignment import Board, make_plan
from scoring.models import Assignment, AssignmentSource, AssignmentStatus, RoundKind, Score

pytestmark = pytest.mark.django_db

FIXTURES = Path(settings.FIXTURES_PATH)
LIVE = [AssignmentStatus.ASSIGNED]


class FakeRequest:
    def __init__(self, user=None):
        self.user = user
        self.META = {"REMOTE_ADDR": "10.0.0.1"}
        self.headers = {}


def close_submissions(event):
    """Submissions closed, judging not started: the organizer's assignment window."""
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=3, hours=1), submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - timedelta(hours=2), judging_starts_at=now + timedelta(hours=1),
        judging_ends_at=now + timedelta(days=3),
    )
    event.refresh_from_db()
    return event


@pytest.fixture
def build(make_event, make_team, make_user):
    """build({"A": 3, "B": 3}, judges=[["A"], ["A"], ["B"], ["B"], []]) -> (event, judges, projects)

    Projects are submitted while the event is open, then submissions close. A judge given `[]`
    covers every track."""

    def make(projects_per_track, judges):
        event = make_event()
        tracks = {name: Track.objects.create(event=event, name=name) for name in projects_per_track}
        projects = []
        for name, n in projects_per_track.items():
            for i in range(n):
                projects.append(Project.objects.create(
                    team=make_team(event), name=f"{name}-{i}", track=tracks[name],
                    status=Status.SUBMITTED, submitted_at=timezone.now(),
                ))
        memberships = []
        for covered in judges:
            m = EventMembership.objects.create(event=event, user=make_user(role=Role.PARTICIPANT), role=Role.JUDGE)
            for name in covered:
                JudgeTrack.objects.create(membership=m, track=tracks[name])
            memberships.append(m)
        return close_submissions(event), memberships, projects

    return make


def run(event, **kw):
    organizer = getattr(event, "organizer", None) or EventMembership.objects.filter(
        event=event, role=Role.ORGANIZER).first().user
    return services.run_assignment(FakeRequest(organizer), event, **kw)


def live_pairs(event):
    return set(Assignment.objects.filter(project__event=event, status__in=LIVE).values_list("judge_id", "project_id"))


# --- the plan -------------------------------------------------------------------------------------


def test_every_project_reaches_the_target_from_judges_of_its_track(build):
    event, judges, projects = build({"A": 3, "B": 3}, [["A"], ["A"], ["B"], ["B"], []])
    round_ = run(event, target=3)
    assert round_.summary["added"] == 18 and round_.summary["short"] == {}
    per_project = Counter(p for _, p in live_pairs(event))
    assert set(per_project.values()) == {3}
    tracks_of = {m.pk: set(m.judge_tracks.values_list("track_id", flat=True)) for m in judges}
    for judge_id, project_id in live_pairs(event):
        project = Project.objects.get(pk=project_id)
        assert not tracks_of[judge_id] or project.track_id in tracks_of[judge_id]
    assert AuditLog.objects.filter(action=AuditAction.ASSIGNMENTS_GENERATED, detail__seed=round_.seed).exists()


def test_load_is_balanced(build):
    event, judges, _ = build({"A": 10}, [[]] * 5)
    run(event, target=3)
    loads = Counter(j for j, _ in live_pairs(event))
    assert max(loads.values()) - min(loads.values()) <= 1  # 30 reviews over 5 judges: 6 each


def test_the_same_seed_gives_the_same_plan_and_another_seed_does_not(build):
    event, _, _ = build({"A": 8}, [[]] * 6)
    first = make_plan(event, target=2, seed=1)
    assert make_plan(event, target=2, seed=1).new == first.new
    assert any(make_plan(event, target=2, seed=s).new != first.new for s in range(2, 6))


def test_running_again_only_tops_up_what_is_missing(build):
    event, _, _ = build({"A": 4}, [[]] * 4)
    run(event, target=2)
    again = run(event, target=2)
    assert again.summary["added"] == 0 and again.kind == RoundKind.TOP_UP
    third = run(event, target=3)
    assert third.summary["added"] == 4


def test_a_load_cap_is_respected_and_what_it_leaves_short_is_reported(build):
    event, judges, _ = build({"A": 4}, [[], []])
    round_ = run(event, target=2, max_load=3)
    assert max(Counter(j for j, _ in live_pairs(event)).values()) <= 3
    assert sum(round_.summary["short"].values()) == 2
    assert any("room for" in w for w in round_.summary["warnings"])


def test_a_track_with_too_few_judges_is_flagged(build):
    event, _, _ = build({"A": 2, "B": 2}, [["A"], ["A"], ["A"], ["B"]])
    round_ = run(event, target=3)
    assert any(w.startswith("B:") for w in round_.summary["warnings"])
    assert round_.summary["short"]


def test_separate_groups_are_linked_by_a_judge_who_covers_both(build):
    # The A and B judges form two islands. The cross-track judge is in neither, so linking them
    # takes two extra reviews: one project on each side.
    event, judges, _ = build({"A": 2, "B": 2}, [["A"], ["A"], ["B"], ["B"], ["A", "B"]])
    Assignment.objects.all().delete()
    # Fill to the target without the cross judge, as if they were added late.
    for judge in judges[:4]:
        for project in Project.objects.filter(event=event, track__judge_tracks__membership=judge):
            Assignment.objects.create(judge=judge, project=project, source=AssignmentSource.MANUAL)
    assert len(Board(event).islands(Board(event).active)) == 2
    round_ = run(event, target=2)
    assert round_.summary["islands_before"] == 2 and round_.summary["islands_after"] == 1
    assert round_.summary["bridges"] == 2
    cross = judges[4]
    tracks = set(Assignment.objects.filter(judge=cross).values_list("project__track__name", flat=True))
    assert tracks == {"A", "B"}


def test_groups_that_cannot_be_linked_are_reported(build):
    event, _, _ = build({"A": 2, "B": 2}, [["A"], ["A"], ["B"], ["B"]])
    round_ = run(event, target=2)
    assert round_.summary["islands_after"] == 2
    assert any("could not be linked" in w for w in round_.summary["warnings"])


def test_each_judges_queue_is_numbered_without_gaps(build):
    event, judges, _ = build({"A": 6}, [[]] * 3)
    run(event, target=2)
    run(event, target=3)
    for judge in judges:
        positions = sorted(Assignment.objects.filter(judge=judge).values_list("position", flat=True))
        assert positions == list(range(len(positions)))


def test_assignment_stops_when_judging_has_ended(build):
    event, _, _ = build({"A": 2}, [[]] * 2)
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(judging_starts_at=now - timedelta(hours=1, minutes=30), judging_ends_at=now - timedelta(hours=1))
    event.refresh_from_db()
    with pytest.raises(services.AssignmentError, match="extend judging"):
        run(event, target=2)


# --- declines, manual changes, stalled judges -------------------------------------------------


def test_a_judge_who_declines_never_gets_the_project_back(build):
    event, judges, projects = build({"A": 1}, [[]] * 3)
    run(event, target=2)
    assignment = Assignment.objects.filter(status=AssignmentStatus.ASSIGNED).first()
    services.decline_assignment(FakeRequest(assignment.judge.user), assignment, "I mentored this team")
    round_ = run(event, target=2)
    assert round_.summary["added"] == 1
    assert (assignment.judge_id, assignment.project_id) not in live_pairs(event)
    assert AuditLog.objects.filter(action=AuditAction.ASSIGNMENT_DECLINED, detail__reason="I mentored this team").exists()


def test_only_the_assigned_judge_can_decline_and_must_say_why(build):
    event, judges, _ = build({"A": 1}, [[]] * 2)
    run(event, target=1)
    assignment = Assignment.objects.get()
    other = next(j for j in judges if j.pk != assignment.judge_id)
    with pytest.raises(services.AssignmentError, match="only the assigned judge"):
        services.decline_assignment(FakeRequest(other.user), assignment, "no")
    with pytest.raises(services.AssignmentError, match="say briefly"):
        services.decline_assignment(FakeRequest(assignment.judge.user), assignment, "  ")


def test_manual_add_follows_the_same_rules(build):
    event, judges, projects = build({"A": 1, "B": 1}, [["A"], ["B"]])
    request = FakeRequest(event.organizer)
    a_project = next(p for p in projects if p.track.name == "A")
    with pytest.raises(services.AssignmentError, match="does not judge the A track"):
        services.add_assignment(request, event, judges[1], a_project)
    services.add_assignment(request, event, judges[0], a_project)
    with pytest.raises(services.AssignmentError, match="already assigned"):
        services.add_assignment(request, event, judges[0], a_project)
    assert Assignment.objects.get().source == AssignmentSource.MANUAL


def test_a_started_review_is_never_withdrawn_or_moved(build):
    event, judges, projects = build({"A": 1}, [[]] * 3)
    run(event, target=1)
    assignment = Assignment.objects.get()
    Score.objects.create(judge=assignment.judge, project=assignment.project)  # a draft
    other = next(j for j in judges if j.pk != assignment.judge_id)
    request = FakeRequest(event.organizer)
    with pytest.raises(services.AssignmentError, match="already started"):
        services.withdraw_assignment(request, event, assignment)
    with pytest.raises(services.AssignmentError, match="already started"):
        services.move_assignment(request, event, assignment, other)
    assert live_pairs(event) == {(assignment.judge_id, assignment.project_id)}


def test_moving_hands_an_unstarted_review_to_another_judge(build):
    event, judges, _ = build({"A": 1}, [[]] * 3)
    run(event, target=1)
    assignment = Assignment.objects.get()
    other = next(j for j in judges if j.pk != assignment.judge_id)
    services.move_assignment(FakeRequest(event.organizer), event, assignment, other)
    assert live_pairs(event) == {(other.pk, assignment.project_id)}
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.WITHDRAWN
    assert AuditLog.objects.filter(action=AuditAction.ASSIGNMENT_MOVED).exists()


def test_a_stalled_judges_unstarted_reviews_go_to_others(build):
    event, judges, projects = build({"A": 4}, [[]] * 3)
    run(event, target=2)
    stalled = judges[0]
    mine = list(Assignment.objects.filter(judge=stalled, status=AssignmentStatus.ASSIGNED))
    Score.objects.create(judge=stalled, project=mine[0].project)  # one started: it stays
    count, round_ = services.reassign_unstarted(FakeRequest(event.organizer), event, stalled)
    assert count == len(mine) - 1 and round_.kind == RoundKind.REASSIGN
    assert list(Assignment.objects.filter(judge=stalled, status=AssignmentStatus.ASSIGNED)
                .values_list("project_id", flat=True)) == [mine[0].project_id]
    assert set(Counter(p for _, p in live_pairs(event)).values()) == {2}


# --- the page ---------------------------------------------------------------------------------------


def test_only_this_events_organizers_reach_the_assignment_page(build, client_for, make_user):
    event, _, _ = build({"A": 2}, [[]] * 2)
    url = f"/organizer/events/{event.slug}/assignments"
    assert client_for(event.organizer).get(url).status_code == 200
    assert client_for(make_user(role=Role.ORGANIZER)).get(url).status_code == 404
    assert client_for(make_user(role=Role.JUDGE)).get(url).status_code == 403


def test_assigning_from_the_page(build, client_for):
    event, judges, projects = build({"A": 3}, [[]] * 3)
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/assignments", {"target": "2", "max_load": "", "seed": "7"}, follow=True)
    assert b"6 reviews assigned" in response.content and b"seed 7" in response.content
    page = client.get(f"/organizer/events/{event.slug}/assignments").content.decode()
    assert "0 of 2 reviews in" in page and "fully assigned" in page and "not started" in page


def test_manual_changes_from_the_page(build, client_for):
    event, judges, projects = build({"A": 1}, [[]] * 3)
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/assignments/add", {"project": projects[0].pk, "judge": judges[0].pk})
    assignment = Assignment.objects.get()
    client.post(f"/organizer/events/{event.slug}/assignments/{assignment.pk}/move", {"judge": judges[1].pk})
    assert live_pairs(event) == {(judges[1].pk, projects[0].pk)}
    moved = Assignment.objects.get(status=AssignmentStatus.ASSIGNED)
    client.post(f"/organizer/events/{event.slug}/assignments/{moved.pk}/withdraw")
    assert live_pairs(event) == set()


def test_the_reassign_button_only_returns_to_this_events_pages(build, client_for):
    event, judges, _ = build({"A": 2}, [[]] * 3)
    run(event, target=1)
    client = client_for(event.organizer)
    url = f"/organizer/events/{event.slug}/judges/{judges[0].pk}/reassign"
    assert client.post(url, {"next": "https://evil.example/"})["Location"] == f"/organizer/events/{event.slug}/assignments"
    assert client.post(url, {"next": f"/organizer/events/{event.slug}/progress"})["Location"] == f"/organizer/events/{event.slug}/progress"


# --- the organizers' data ---------------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURES.exists(), reason="acceptance/fixtures.json not present")
def test_the_fixture_is_topped_up_to_three_reviews_without_touching_the_imported_ones(make_user):
    import_file(FIXTURES)
    event = Event.objects.get()
    event.organizer = make_user(role=Role.ORGANIZER)
    EventMembership.objects.create(event=event, user=event.organizer, role=Role.ORGANIZER)
    with pytest.raises(services.AssignmentError, match="extend judging"):
        run(event, target=3)
    from events.services import extend_judging

    extend_judging(FakeRequest(event.organizer), event, timezone.now() + timedelta(days=7), "top up the fixture")
    before = live_pairs(event)
    round_ = run(event, target=3, seed=2026)
    assert round_.summary["added"] == 8 and round_.summary["short"] == {}  # the 8 two-review projects
    assert round_.summary["islands_after"] == 1
    assert before <= live_pairs(event)
    assert set(Counter(p for _, p in live_pairs(event)).values()) >= {3}
