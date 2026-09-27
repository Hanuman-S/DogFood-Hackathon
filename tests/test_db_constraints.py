"""Rules the database itself enforces, proved by writing *around* the service layer.

A service check cannot win a race between two simultaneous requests, and a future code path can
forget to call it. A constraint does neither. So every test here writes through the ORM with no
service function in the way and expects an `IntegrityError` from Postgres.

Each violation is wrapped in `transaction.atomic()`, because a failed statement poisons the
surrounding transaction in Postgres.
"""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.roles import Role
from events.models import Event, EventMembership, Track
from projects.models import Project, Status
from scoring.models import Criterion, Score
from teams.models import Team, TeamMember

pytestmark = pytest.mark.django_db


def refused(create):
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


@pytest.fixture
def event(make_event):
    return make_event()


@pytest.fixture
def user(make_user):
    return make_user()


# --- the conflict-of-interest rule (exclusion constraint) ----------------------------------------


@pytest.mark.parametrize("staff", [Role.JUDGE, Role.ORGANIZER])
def test_a_participant_cannot_also_be_staff_in_the_same_event(event, user, staff):
    EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT)
    refused(lambda: EventMembership.objects.create(user=user, event=event, role=staff))


@pytest.mark.parametrize("staff", [Role.JUDGE, Role.ORGANIZER])
def test_the_rule_holds_in_the_other_order_too(event, user, staff):
    EventMembership.objects.create(user=user, event=event, role=staff)
    refused(lambda: EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT))


def test_judge_and_organizer_together_is_allowed(event, user):
    EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)
    EventMembership.objects.create(user=user, event=event, role=Role.ORGANIZER)
    assert EventMembership.objects.filter(user=user, event=event).count() == 2


def test_competing_in_one_event_and_judging_another_is_allowed(make_event, user):
    one, two = make_event(), make_event()
    EventMembership.objects.create(user=user, event=one, role=Role.PARTICIPANT)
    EventMembership.objects.create(user=user, event=two, role=Role.JUDGE)
    assert EventMembership.objects.filter(user=user).count() == 2


def test_the_same_role_cannot_be_recorded_twice(event, user):
    EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)
    refused(lambda: EventMembership.objects.create(user=user, event=event, role=Role.JUDGE))


def test_the_side_column_is_computed_by_the_database(make_event, user):
    """`side` is a stored generated column, so application code cannot write an inconsistent
    value that would fool the exclusion constraint."""
    one, two = make_event(), make_event()
    competitor = EventMembership.objects.create(user=user, event=one, role=Role.PARTICIPANT)
    staff = EventMembership.objects.create(user=user, event=two, role=Role.JUDGE)
    competitor.refresh_from_db()
    staff.refresh_from_db()
    assert (competitor.side, staff.side) == ("competitor", "staff")


# --- teams and projects ---------------------------------------------------------------------------


def test_one_team_per_person_per_event(event, user):
    first = Team.objects.create(event=event, name="First", captain=user)
    second = Team.objects.create(event=event, name="Second", captain=user)
    TeamMember.objects.create(team=first, user=user)
    refused(lambda: TeamMember.objects.create(team=second, user=user))


def test_team_names_are_unique_within_an_event_in_any_case(event, user, make_user):
    Team.objects.create(event=event, name="Night Owls", captain=user)
    refused(lambda: Team.objects.create(event=event, name="NIGHT OWLS", captain=make_user()))


def test_a_team_has_one_project(event, user):
    team = Team.objects.create(event=event, name="Solo", captain=user)
    Project.objects.create(team=team, name="One")
    refused(lambda: Project.objects.create(team=team, name="Two"))


def test_a_submitted_project_must_carry_a_submission_time(event, user):
    team = Team.objects.create(event=event, name="T", captain=user)
    refused(lambda: Project.objects.create(team=team, name="P", status=Status.SUBMITTED, submitted_at=None))


# --- event timeline ---------------------------------------------------------------------------


def _event(now=None, **dates):
    """A valid, strictly ordered timeline, with `dates` overriding any of it."""
    now = now or timezone.now()
    values = dict(
        starts_at=now - timedelta(hours=1), submissions_open_at=now,
        submissions_close_at=now + timedelta(days=1), judging_starts_at=now + timedelta(days=1, hours=1),
        judging_ends_at=now + timedelta(days=2),
    )
    values.update(dates)
    return Event.objects.create(slug=f"e-{Event.objects.count()}", name="E", **values)


def test_a_strictly_ordered_timeline_is_accepted_with_or_without_results():
    now = timezone.now()
    _event()
    _event(results_at=now + timedelta(days=3))


@pytest.mark.parametrize("field, offset", [
    ("starts_at", timedelta(0)),  # event starts == submissions open
    ("judging_starts_at", timedelta(days=1)),  # judging starts == submissions close
    ("judging_ends_at", timedelta(days=1, hours=1)),  # judging ends == judging starts
    ("results_at", timedelta(days=2)),  # results == judging ends
])
def test_each_date_must_be_strictly_after_the_one_before(field, offset):
    now = timezone.now()
    refused(lambda: _event(now=now, **{field: now + offset}))


def test_submissions_cannot_close_before_they_open():
    now = timezone.now()
    refused(lambda: _event(submissions_open_at=now + timedelta(hours=1), submissions_close_at=now))


def test_a_zero_length_submission_window_is_refused():
    now = timezone.now()
    refused(lambda: _event(submissions_open_at=now, submissions_close_at=now))


def test_judging_cannot_start_before_submissions_close():
    now = timezone.now()
    refused(lambda: _event(submissions_close_at=now + timedelta(days=1, hours=2)))


def test_track_names_are_unique_within_an_event_but_not_across_events(make_event):
    one, two = make_event(), make_event()
    Track.objects.create(event=one, name="Security")
    Track.objects.create(event=two, name="Security")
    refused(lambda: Track.objects.create(event=one, name="Security"))


# --- scoring ----------------------------------------------------------------------------------


def test_one_review_per_judge_per_project(event, user, make_team):
    judge = EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)
    project = Project.objects.create(team=make_team(event), name="P")
    Score.objects.create(judge=judge, project=project)
    refused(lambda: Score.objects.create(judge=judge, project=project))


def test_a_criterion_key_is_unique_per_event_and_min_is_below_max(event):
    Criterion.objects.create(event=event, key="quality", label="Quality")
    refused(lambda: Criterion.objects.create(event=event, key="quality", label="Again"))
    refused(lambda: Criterion.objects.create(event=event, key="odd", label="Odd", min_value=5, max_value=5))
