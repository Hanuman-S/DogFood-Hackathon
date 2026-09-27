"""T2 shared models: assignments, submitted reviews, judge invites, and the backfill that brings
existing data up to them. The rules here are enforced by the database, so these tests write
rows directly rather than going through services."""

import importlib
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.roles import Role
from events.models import EventMembership, JudgeInvite
from imports.fixtures import import_file
from projects.models import Project, Status
from scoring.models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    Score,
)

pytestmark = pytest.mark.django_db

FIXTURES = Path(settings.FIXTURES_PATH)
needs_file = pytest.mark.skipif(not FIXTURES.exists(), reason="acceptance/fixtures.json not present")


def test_the_models_and_migrations_agree():
    """A model change without its migration would only show up on someone else's machine."""
    out = StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


@pytest.fixture
def judged_project(make_event, make_team, make_user):
    """An event with one submitted project and one judge of that event."""
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(
        team=team, name="Quiet Hours", status=Status.SUBMITTED, submitted_at=timezone.now()
    )
    judge = EventMembership.objects.create(event=event, user=make_user(), role=Role.JUDGE)
    return event, project, judge


def assign(judge, project, **fields):
    return Assignment.objects.create(
        judge=judge, project=project, source=fields.pop("source", AssignmentSource.MANUAL), **fields
    )


def test_one_live_assignment_per_judge_and_project(judged_project):
    _, project, judge = judged_project
    assign(judge, project)
    with pytest.raises(IntegrityError), transaction.atomic():
        assign(judge, project)


def test_a_declined_assignment_blocks_reassigning_the_same_judge(judged_project):
    """Declining is a conflict of interest: the same judge must not get the project back."""
    _, project, judge = judged_project
    assign(judge, project, status=AssignmentStatus.DECLINED)
    with pytest.raises(IntegrityError), transaction.atomic():
        assign(judge, project)


def test_a_withdrawn_assignment_is_history_and_can_be_given_again(judged_project):
    _, project, judge = judged_project
    assign(judge, project, status=AssignmentStatus.WITHDRAWN)
    assign(judge, project, status=AssignmentStatus.WITHDRAWN)
    assign(judge, project)
    assert Assignment.objects.filter(judge=judge, project=project).count() == 3


def test_the_database_refuses_an_unknown_status_or_source(judged_project):
    _, project, judge = judged_project
    with pytest.raises(IntegrityError), transaction.atomic():
        assign(judge, project, status="done")
    with pytest.raises(IntegrityError), transaction.atomic():
        assign(judge, project, source="magic")


def test_only_a_judge_of_the_projects_event_can_be_assigned(judged_project, make_event, make_user):
    event, project, _ = judged_project
    organizer = EventMembership.objects.get(event=event, role=Role.ORGANIZER)
    with pytest.raises(ValidationError):
        Assignment(judge=organizer, project=project, source=AssignmentSource.MANUAL).clean()
    elsewhere = EventMembership.objects.create(event=make_event(), user=make_user(), role=Role.JUDGE)
    with pytest.raises(ValidationError):
        Assignment(judge=elsewhere, project=project, source=AssignmentSource.MANUAL).clean()


def test_a_review_is_a_draft_until_submitted(judged_project):
    _, project, judge = judged_project
    score = Score.objects.create(judge=judge, project=project)
    assert score.submitted_at is None


# --- judge invites ------------------------------------------------------------------------------


def invite(event, email="new.judge@example.org", digest="a" * 64, **fields):
    return JudgeInvite.objects.create(
        event=event, email=email, digest=digest,
        expires_at=fields.pop("expires_at", timezone.now() + timedelta(days=7)), **fields,
    )


def test_one_pending_invite_per_person_per_event(make_event):
    event = make_event()
    invite(event)
    with pytest.raises(IntegrityError), transaction.atomic():
        invite(event, digest="b" * 64)
    # Once the first is revoked (or accepted), a new one may be issued.
    JudgeInvite.objects.filter(event=event).update(revoked_at=timezone.now())
    invite(event, digest="c" * 64)


def test_an_invite_cannot_be_both_accepted_and_revoked(make_event):
    now = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        invite(make_event(), accepted_at=now, revoked_at=now)


def test_only_unexpired_unused_invites_are_open(make_event):
    event = make_event()
    now = timezone.now()
    live = invite(event, email="a@example.org", digest="1" * 64)
    invite(event, email="b@example.org", digest="2" * 64, expires_at=now - timedelta(seconds=1))
    invite(event, email="c@example.org", digest="3" * 64, revoked_at=now)
    invite(event, email="d@example.org", digest="4" * 64, accepted_at=now)
    assert list(JudgeInvite.objects.open()) == [live]


# --- the importer and the backfill ---------------------------------------------------------------


@needs_file
def test_imported_reviews_are_submitted_and_each_has_its_assignment():
    import_file(FIXTURES)
    scores = Score.objects.all()
    assert scores.count() == 123
    assert not scores.filter(submitted_at__isnull=True).exists()
    assignments = Assignment.objects.all()
    assert assignments.count() == 123
    assert set(assignments.values_list("source", "status").distinct()) == {
        (AssignmentSource.IMPORT, AssignmentStatus.ASSIGNED)
    }
    assert set(assignments.values_list("judge_id", "project_id")) == set(
        scores.values_list("judge_id", "project_id")
    )
    # Each judge's queue is numbered 0..n-1, in file order.
    for judge_id in assignments.values_list("judge_id", flat=True).distinct():
        positions = sorted(assignments.filter(judge_id=judge_id).values_list("position", flat=True))
        assert positions == list(range(len(positions)))


@needs_file
def test_a_second_import_creates_no_extra_assignments():
    import_file(FIXTURES)
    import_file(FIXTURES)
    assert Assignment.objects.count() == 123


def test_the_backfill_submits_existing_reviews_and_gives_them_assignments(judged_project, make_user):
    """What the migration does to a database that already held imported reviews."""
    event, project, judge = judged_project
    other = EventMembership.objects.create(event=event, user=make_user(), role=Role.JUDGE)
    first = Score.objects.create(judge=judge, project=project)
    Score.objects.create(judge=other, project=project, submitted_at=timezone.now())
    assign(other, project, source=AssignmentSource.IMPORT)  # already has one: left alone

    migration = importlib.import_module(
        "scoring.migrations.0002_rubric_descriptions_assignments_submitted_at"
    )
    migration.backfill_existing_reviews(apps, None)

    first.refresh_from_db()
    assert first.submitted_at == first.created_at
    assert Assignment.objects.filter(project=project, status=AssignmentStatus.ASSIGNED).count() == 2
    assert Assignment.objects.get(judge=judge).source == AssignmentSource.IMPORT
