"""The demo seed's judging event (Dogfood Archive 2026): in its judging phase at boot, five
submitted projects from demo teams, and one fixed-seed assignment round that gives both demo judges
a queue. Built through the real services; create-only and idempotent."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership, Phase
from projects.models import Project, Status
from scoring.models import Assignment, AssignmentStatus

pytestmark = pytest.mark.django_db
TOKENS = {"admin": "t-admin", "organizer": "t-org", "judge_a": "t-ja", "judge_b": "t-jb", "participant": "t-p"}


def seed():
    with override_settings(DEMO_MODE=True, DEMO_TOKENS=TOKENS):
        out = StringIO()
        call_command("seed_demo", stdout=out)
        return out.getvalue()


def queue(email, event):
    membership = EventMembership.objects.get(event=event, role=Role.JUDGE, user__email=email)
    return set(Assignment.objects.filter(judge=membership, status=AssignmentStatus.ASSIGNED)
               .values_list("project__name", flat=True))


def test_phases_of_the_demo_events_at_first_boot():
    seed()
    now = timezone.now()
    phases = {e.slug: e.phase_at(now) for e in Event.objects.all()}
    assert phases == {"dogfood-live-demo": Phase.OPEN, "dogfood-archive-2026": Phase.JUDGING}


def test_the_archive_has_five_submitted_projects_and_both_judges_have_a_queue():
    out = seed()
    event = Event.objects.get(slug="dogfood-archive-2026")
    projects = Project.objects.filter(event=event)
    assert projects.count() == 5 and all(p.status == Status.SUBMITTED for p in projects)
    assert all(p.submitted_at < event.submissions_close_at for p in projects)
    names = set(projects.values_list("name", flat=True))
    for email in ("judge.a@dogfood.local", "judge.b@dogfood.local"):
        assert queue(email, event) == names          # target 2 with two judges: both see all five
    (round_,) = event.assignment_rounds.all()
    assert round_.seed == 20260926 and round_.target_reviews == 2
    assert "assignment round run" in out


def test_the_archive_was_built_through_the_participant_services():
    seed()
    event = Event.objects.get(slug="dogfood-archive-2026")
    for action in (AuditAction.TEAM_CREATED, AuditAction.PROJECT_CREATED, AuditAction.PROJECT_SUBMITTED):
        assert AuditLog.objects.filter(action=action).count() >= 5, action
    assert AuditLog.objects.filter(action=AuditAction.ASSIGNMENTS_GENERATED, subject=event.slug).count() == 1


def test_seeding_twice_changes_nothing():
    seed()
    event = Event.objects.get(slug="dogfood-archive-2026")
    before = (Project.objects.count(), Assignment.objects.count(), event.assignment_rounds.count(),
              EventMembership.objects.count())
    out = seed()
    assert (Project.objects.count(), Assignment.objects.count(), event.assignment_rounds.count(),
            EventMembership.objects.count()) == before
    assert "closed demo event" in out and "[exists]" in out


def test_the_demo_judges_see_their_queue_in_the_portal(client_for):
    from accounts.models import User

    seed()
    judge = User.objects.get(email="judge.a@dogfood.local")
    judge.set_password("correct-horse-battery")
    judge.save()
    page = client_for(judge).get("/judge/events/dogfood-archive-2026/")
    assert page.status_code == 200 and b"Quiet Map" in page.content
