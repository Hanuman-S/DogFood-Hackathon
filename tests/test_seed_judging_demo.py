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


# --- the archive's community vote (T3) ------------------------------------------------------------------

def test_archive_vote_is_seeded_open_with_weights_and_a_flagged_cluster():
    from scoring.services import final_weights
    from voting import integrity
    from voting.models import Ballot, VotingConfig

    seed()
    event = Event.objects.get(slug="dogfood-archive-2026")
    config = VotingConfig.objects.get(event=event)
    now = timezone.now()
    assert config.opens_at <= now < config.closes_at and config.method == "quadratic"
    assert final_weights(event) == (80, 20)
    assert AuditLog.objects.filter(action=AuditAction.WEIGHTS_BYPASSED, subject=event.slug).exists()
    assert Ballot.objects.filter(event=event).count() == 10
    kinds = {f.kind for f in integrity.flags(event)}
    assert {"ip_burst", "identical_ballots"} <= kinds
    burst = next(f for f in integrity.flags(event) if f.kind == "ip_burst")
    assert {b.voter_user.email for b in burst.ballots} == {f"voter.cluster{i}@dogfood.local" for i in range(1, 5)}
    seed()  # create-only
    assert Ballot.objects.filter(event=event).count() == 10


def test_demo_participant_can_vote_in_the_archive():
    from accounts.models import User
    from voting import services as voting

    seed()
    event = Event.objects.get(slug="dogfood-archive-2026")
    participant = User.objects.get(email="participant@dogfood.local")
    config = voting.voting_for(event)
    assert voting.ineligibility(event, config, participant) is None
    other = Project.objects.filter(event=event).exclude(team__members__user=participant).first()
    voting.cast(event, voting.Voter(participant), "", {other.pk: 4}, participant)


def test_fixture_event_gets_a_closed_vote_after_import():
    from imports.fixtures import import_file
    from voting.models import VotingConfig
    from voting import services as voting

    seed()
    import_file()
    with override_settings(DEMO_MODE=True, DEMO_TOKENS=TOKENS):
        out = StringIO()
        call_command("seed_demo", "--votes", stdout=out)
        assert "created" in out.getvalue()
        call_command("seed_demo", "--votes", stdout=out)
    config = VotingConfig.objects.exclude(event__slug__startswith="dogfood-").get()
    assert voting.state(config, timezone.now()) == "closed"


# --- the old "demo" tag (a meaningless filter chip in the gallery) ---------------------------------

def test_demo_projects_are_seeded_without_a_tag():
    from projects.models import Tag

    seed()
    assert not Tag.objects.filter(name="demo").exists()
    assert not Project.objects.filter(event__slug="dogfood-archive-2026", tags__isnull=False).exists()


def test_a_reboot_clears_the_old_tag_from_the_demo_events_only(make_event, make_team):
    from core.deadlines import deadline_bypass
    from projects.models import Tag

    seed()
    old = Tag.objects.create(name="demo")
    with deadline_bypass(None, "test: a database seeded by the old seed"):
        for project in Project.objects.filter(event__slug__in=["dogfood-live-demo", "dogfood-archive-2026"]):
            project.tags.add(old)
    # A real event that happens to use the tag keeps it.
    other = make_event(slug="real-event")
    mine = Project.objects.create(team=make_team(other), event=other, name="Mine")
    mine.tags.add(old)

    seed()
    assert list(old.projects.all()) == [mine]
    mine.tags.clear()
    seed()
    assert not Tag.objects.filter(name="demo").exists()
