"""Once judging has started and the event has judging work (an assignment or a score), the submission
and judging-start dates are frozen: moving them would reopen submissions under the judges. An event
with no assignments and no scores stays fixable. Behind it, Score.project is RESTRICT: a reviewed
project never goes with its team."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.db.models import RestrictedError
from django.test import RequestFactory
from django.utils import timezone

from _dates import dt_fields
from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.forms import EventForm
from events.models import Event, EventMembership
from events.services import EventRuleError, timeline_locked, update_event
from projects.models import Project, Status
from scoring.models import Assignment, AssignmentSource, Criterion, Score
from teams.models import TeamMember
from teams.services import TeamRuleError, leave_team

pytestmark = pytest.mark.django_db


def minute(dt):
    return dt.replace(second=0, microsecond=0)


@pytest.fixture
def judging(make_event):
    """An event whose judging started yesterday (dates on whole minutes, as the form posts them)."""
    now = minute(timezone.now())
    event = make_event(submissions_open_at=now - timedelta(days=3), submissions_close_at=now - timedelta(days=2),
                       judging_starts_at=now - timedelta(days=1), judging_ends_at=now + timedelta(days=2),
                       starts_at=now - timedelta(days=4))
    Event.objects.filter(pk=event.pk).update(tagline="t", description="d")
    event.refresh_from_db()
    event.organizer = EventMembership.objects.get(event=event, role=Role.ORGANIZER).user
    return event


def post_data(event, **dates):
    data = {"name": event.name, "slug": event.slug, "tagline": event.tagline, "description": event.description,
            "min_team_size": event.min_team_size, "max_team_size": event.max_team_size}
    for name in ("starts_at", "submissions_open_at", "submissions_close_at", "judging_starts_at", "judging_ends_at"):
        data.update(dt_fields(name, dates.get(name, getattr(event, name))))
    data.update(dt_fields("results_at", ""))
    return data


def save(event, locked_form=False, **dates):
    """update_event with a form built without the lock (as a crafted POST would be), so the service's
    own check is what answers."""
    request = RequestFactory().post("/")
    request.user = event.organizer
    form = EventForm(post_data(event, **dates), instance=event, timeline_locked=locked_form)
    assert form.is_valid(), form.errors
    return update_event(request, event, form)


def give_assignment(event, make_team, make_user):
    from core.deadlines import deadline_bypass
    judge = EventMembership.objects.create(user=make_user(role=Role.JUDGE), event=event, role=Role.JUDGE)
    with deadline_bypass(None, "test: a team after the close"):  # setup only: the event is past its close
        project = Project.objects.create(team=make_team(event), name="P", status=Status.SUBMITTED,
                                         submitted_at=event.submissions_close_at - timedelta(hours=1))
    return Assignment.objects.create(judge=judge, project=project, source=AssignmentSource.MANUAL)


def test_an_empty_event_can_still_be_corrected_after_judging_starts(judging):
    later = judging.submissions_close_at + timedelta(hours=6)
    assert not timeline_locked(judging)
    save(judging, submissions_close_at=later)
    judging.refresh_from_db()
    assert judging.submissions_close_at == later
    assert AuditLog.objects.filter(action=AuditAction.EVENT_UPDATED, subject=judging.slug).exists()


def test_a_single_assignment_freezes_the_timeline(judging, make_team, make_user):
    give_assignment(judging, make_team, make_user)
    assert timeline_locked(judging)
    old = {f: getattr(judging, f) for f in ("submissions_open_at", "submissions_close_at", "judging_starts_at")}
    for field, value in (("submissions_close_at", old["submissions_close_at"] + timedelta(hours=12)),
                         ("submissions_close_at", old["submissions_close_at"] - timedelta(hours=1)),
                         ("submissions_open_at", old["submissions_open_at"] - timedelta(hours=1)),
                         ("judging_starts_at", old["judging_starts_at"] + timedelta(hours=1))):
        with pytest.raises(EventRuleError) as caught:
            save(judging, **{field: value})
        assert "frozen" in str(caught.value)
        judging.refresh_from_db()
        assert {f: getattr(judging, f) for f in old} == old
    refusals = AuditLog.objects.filter(action=AuditAction.EVENT_CHANGE_REFUSED, detail__reason="judging has started")
    assert refusals.count() == 4


def test_a_score_alone_freezes_it_too(judging, make_team, make_user):
    assignment = give_assignment(judging, make_team, make_user)
    Score.objects.create(judge=assignment.judge, project=assignment.project)
    assignment.delete()
    assert timeline_locked(judging)


def test_the_end_of_judging_stays_editable(judging, make_team, make_user):
    give_assignment(judging, make_team, make_user)
    later = judging.judging_ends_at + timedelta(days=1)
    save(judging, judging_ends_at=later)
    judging.refresh_from_db()
    assert judging.judging_ends_at == later


def test_before_judging_starts_nothing_is_frozen(make_event, make_team, make_user):
    event = make_event()
    give_assignment(event, make_team, make_user)
    assert not timeline_locked(event)


def test_the_settings_form_shows_the_fields_locked_and_ignores_posted_dates(judging, make_team, make_user,
                                                                          client_for):
    give_assignment(judging, make_team, make_user)
    client = client_for(judging.organizer)
    page = client.get(f"/organizer/events/{judging.slug}/").content.decode()
    assert page.count("locked: judging has started") == 3
    old_close = judging.submissions_close_at
    client.post(f"/organizer/events/{judging.slug}/",
                post_data(judging, submissions_close_at=old_close + timedelta(days=9)))
    judging.refresh_from_db()
    assert judging.submissions_close_at == old_close  # a disabled field keeps its stored value


# --- the RESTRICT backstop -----------------------------------------------------------------------------

@pytest.fixture
def reviewed_draft(make_event, make_team, make_user):
    """A team of one whose (draft) project has a review, in an event still open for submissions."""
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, name="Reviewed", status=Status.DRAFT)
    judge = EventMembership.objects.create(user=make_user(role=Role.JUDGE), event=event, role=Role.JUDGE)
    Criterion.objects.create(event=event, key="impact", label="Impact", weight=Decimal("1"))
    Score.objects.create(judge=judge, project=project)
    return event, team, project


def test_an_orm_delete_of_a_reviewed_project_is_refused(reviewed_draft):
    _, _, project = reviewed_draft
    with pytest.raises(RestrictedError), transaction.atomic():
        project.delete()
    assert Project.objects.filter(pk=project.pk).exists()


def test_the_last_member_leaving_a_reviewed_project_is_refused_and_nothing_is_deleted(reviewed_draft):
    event, team, project = reviewed_draft
    captain = team.captain
    request = RequestFactory().post("/")
    request.user = captain
    with pytest.raises(TeamRuleError) as caught:
        leave_team(request, team)
    assert "has reviews" in str(caught.value)
    assert Project.objects.filter(pk=project.pk).exists() and Score.objects.filter(project=project).exists()
    assert TeamMember.objects.filter(team=team, user=captain).exists()
    assert EventMembership.objects.filter(event=event, user=captain, role=Role.PARTICIPANT).exists()


def row_counts():
    from django.apps import apps
    return {m._meta.label: m.objects.count() for m in apps.get_models()}


def test_an_event_with_a_review_cannot_be_deleted_and_nothing_goes(reviewed_draft):
    """Reviews are permanent: Score.project and Score.judge are RESTRICT, and nothing cascades to a
    Score, so deleting its event is refused -- whole, with nothing half-deleted."""
    from core.deadlines import deadline_bypass
    event, _, project = reviewed_draft
    before = row_counts()
    with pytest.raises(RestrictedError), deadline_bypass(None, "test: try to delete the event"):
        event.delete()
    assert row_counts() == before


def test_an_event_without_reviews_still_cascades(make_event, make_team):
    from core.deadlines import deadline_bypass
    event = make_event()
    project = Project.objects.create(team=make_team(event), name="Unreviewed", status=Status.DRAFT)
    with deadline_bypass(None, "test: delete event"):
        event.delete()
    assert not Project.objects.filter(pk=project.pk).exists()
