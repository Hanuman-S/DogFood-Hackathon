"""Constraints that must hold at the database level.

The brief is specific about this: the conflict-of-interest rule and one-team-per-user-per-event
must be enforced by the database, not only by the service layer. A service check cannot win a
race between two simultaneous requests; a unique index can.

Every test here writes through the ORM with validation bypassed -- `Model.objects.create`, never
a service function -- because the point is to prove the *database* refuses, not that our Python
refuses first. Each assertion therefore expects an `IntegrityError`.

Note on transactions: a failed statement poisons the surrounding transaction in Postgres, so
each violation is wrapped in `transaction.atomic()` and the test uses
`django_db(transaction=True)` where it needs to continue afterwards.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from core import clock
from events.models import Event, EventMembership, Role, Track
from projects.models import Project, ProjectStatus
from teams.models import Team, TeamMember


@pytest.fixture
def event(db):
    now = clock.now()
    return Event.objects.create(
        name="Constraint Test Event",
        slug="constraint-test-event",
        starts_at=now - dt.timedelta(days=1),
        submissions_open_at=now - dt.timedelta(days=1),
        submissions_close_at=now + dt.timedelta(days=1),
        judging_ends_at=now + dt.timedelta(days=8),
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="constraint@example.org", display_name="Constraint")


# --------------------------------------------------------------------------------------
# one team per user per event
# --------------------------------------------------------------------------------------


def test_one_team_per_user_per_event_is_enforced_by_the_database(event, user):
    """The rule most likely to be broken by a race: two invite redemptions arriving together.

    A check-then-insert in the service layer cannot prevent it. The unique index can.
    """
    first = Team.objects.create(event=event, name="First Team")
    second = Team.objects.create(event=event, name="Second Team")

    TeamMember.objects.create(team=first, user=user, event=event, is_captain=True)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TeamMember.objects.create(team=second, user=user, event=event)


def test_the_same_user_may_join_a_team_in_a_different_event(event, user):
    """The constraint is scoped to one event, not global. Competing in two hackathons is normal."""
    other = Event.objects.create(
        name="Another Event",
        slug="another-event",
        starts_at=event.starts_at,
        submissions_open_at=event.submissions_open_at,
        submissions_close_at=event.submissions_close_at,
    )
    team_here = Team.objects.create(event=event, name="Here")
    team_there = Team.objects.create(event=other, name="There")

    TeamMember.objects.create(team=team_here, user=user, event=event, is_captain=True)
    TeamMember.objects.create(team=team_there, user=user, event=other, is_captain=True)

    assert TeamMember.objects.filter(user=user).count() == 2


def test_a_team_cannot_have_two_captains(event, user):
    team = Team.objects.create(event=event, name="Two Captains")
    other = User.objects.create_user(email="second@example.org", display_name="Second")

    TeamMember.objects.create(team=team, user=user, event=event, is_captain=True)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TeamMember.objects.create(team=team, user=other, event=event, is_captain=True)


# --------------------------------------------------------------------------------------
# conflict of interest
# --------------------------------------------------------------------------------------


def test_a_participant_cannot_also_be_a_judge_in_the_same_event(event, user):
    """Enforced by an exclusion constraint, so it holds against concurrent inserts."""
    EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)


def test_a_participant_cannot_also_be_an_organizer_in_the_same_event(event, user):
    EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EventMembership.objects.create(user=user, event=event, role=Role.ORGANIZER)


def test_the_rule_applies_in_the_other_order_too(event, user):
    """Inserting the staff role first must also block the participant role."""
    EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT)


def test_judge_and_organizer_together_is_allowed(event, user):
    """Both are staff, so there is no conflict. A small hackathon's organizer often judges too.

    This is why the constraint is an exclusion on `side` rather than a unique index on
    (user, event): a unique index would forbid this legitimate combination.
    """
    EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)
    EventMembership.objects.create(user=user, event=event, role=Role.ORGANIZER)

    assert EventMembership.objects.filter(user=user, event=event).count() == 2


def test_the_same_user_may_compete_in_one_event_and_judge_another(event, user):
    other = Event.objects.create(
        name="Judged Event",
        slug="judged-event",
        starts_at=event.starts_at,
        submissions_open_at=event.submissions_open_at,
        submissions_close_at=event.submissions_close_at,
    )
    EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT)
    EventMembership.objects.create(user=user, event=other, role=Role.JUDGE)

    assert EventMembership.objects.filter(user=user).count() == 2


def test_the_same_role_cannot_be_recorded_twice(event, user):
    EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EventMembership.objects.create(user=user, event=event, role=Role.JUDGE)


def test_the_side_column_is_computed_by_the_database(event, user):
    """`side` is a stored generated column, so the exclusion constraint cannot be fooled by
    application code writing an inconsistent value."""
    membership = EventMembership.objects.create(user=user, event=event, role=Role.PARTICIPANT)
    membership.refresh_from_db()
    assert membership.side == "competitor"

    judge_event = Event.objects.create(
        name="Side Test",
        slug="side-test",
        starts_at=event.starts_at,
        submissions_open_at=event.submissions_open_at,
        submissions_close_at=event.submissions_close_at,
    )
    judge = EventMembership.objects.create(user=user, event=judge_event, role=Role.JUDGE)
    judge.refresh_from_db()
    assert judge.side == "staff"


# --------------------------------------------------------------------------------------
# one active project per team
# --------------------------------------------------------------------------------------


def test_a_team_cannot_have_two_active_projects(event, user):
    team = Team.objects.create(event=event, name="Busy Team")
    Project.objects.create(event=event, team=team, name="First")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Project.objects.create(event=event, team=team, name="Second")


def test_a_flagged_duplicate_may_coexist_with_the_project_it_duplicates(event, user):
    """The partial index is what makes "keep both rows" compatible with "one project per team".

    This is exactly the fixture's prj_07 / prj_41 situation.
    """
    team = Team.objects.create(event=event, name="Double Submitter")
    canonical = Project.objects.create(
        event=event,
        team=team,
        name="Dry Harbour",
        status=ProjectStatus.SUBMITTED,
        submitted_at=clock.now(),
    )
    duplicate = Project.objects.create(
        event=event,
        team=team,
        name="Dry Harbour",
        status=ProjectStatus.SUBMITTED,
        submitted_at=clock.now(),
        duplicate_of=canonical,
    )

    assert Project.objects.filter(team=team).count() == 2
    assert Project.objects.filter(team=team).canonical().count() == 1
    assert duplicate.duplicate_of_id == canonical.pk


# --------------------------------------------------------------------------------------
# status / timestamp coherence
# --------------------------------------------------------------------------------------


def test_a_submitted_project_must_carry_a_submission_time(event):
    team = Team.objects.create(event=event, name="Timestamp Team")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Project.objects.create(
                event=event, team=team, name="No Timestamp", status=ProjectStatus.SUBMITTED
            )


def test_a_draft_must_not_carry_a_submission_time(event):
    team = Team.objects.create(event=event, name="Draft Team")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Project.objects.create(
                event=event,
                team=team,
                name="Premature",
                status=ProjectStatus.DRAFT,
                submitted_at=clock.now(),
            )


# --------------------------------------------------------------------------------------
# event timeline
# --------------------------------------------------------------------------------------


def test_an_event_cannot_close_submissions_before_it_opens_them(db):
    now = clock.now()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Event.objects.create(
                name="Backwards",
                slug="backwards",
                starts_at=now,
                submissions_open_at=now + dt.timedelta(days=2),
                submissions_close_at=now + dt.timedelta(days=1),
            )


def test_judging_cannot_end_before_submissions_close(db):
    now = clock.now()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Event.objects.create(
                name="Early Judging",
                slug="early-judging",
                starts_at=now,
                submissions_open_at=now,
                submissions_close_at=now + dt.timedelta(days=2),
                judging_ends_at=now + dt.timedelta(days=1),
            )


def test_a_zero_length_submission_window_is_refused(db):
    """`submissions_open_at == submissions_close_at` would make the window empty, and the guard's
    half-open semantics would refuse every write -- better caught at write time."""
    now = clock.now()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Event.objects.create(
                name="Instant",
                slug="instant",
                starts_at=now,
                submissions_open_at=now,
                submissions_close_at=now,
            )


def test_track_names_are_unique_within_an_event_but_not_across_events(event):
    other = Event.objects.create(
        name="Other Tracks",
        slug="other-tracks",
        starts_at=event.starts_at,
        submissions_open_at=event.submissions_open_at,
        submissions_close_at=event.submissions_close_at,
    )
    Track.objects.create(event=event, name="Accessibility")
    # Same name, different event: fine.
    Track.objects.create(event=other, name="Accessibility")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Track.objects.create(event=event, name="Accessibility")
