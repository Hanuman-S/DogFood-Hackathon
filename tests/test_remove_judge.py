"""Removing a judge never deletes submitted reviews: remove_judge refuses a judge with one (409,
audited, in any phase), and Score.judge is RESTRICT behind it (refused unless the whole event goes). Drafts and assignments go, counted."""

from decimal import Decimal

import pytest
from django.db import transaction
from django.db.models import RestrictedError
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import EventMembership
from events.services import JudgeHasReviews, remove_judge
from projects.models import Project, Status
from scoring.models import Assignment, AssignmentSource, Criterion, Score, ScoreItem

pytestmark = pytest.mark.django_db


@pytest.fixture
def judged(make_event, make_team, make_user):
    event = make_event()
    judge = make_user(role=Role.JUDGE, email="judge.x@example.org")
    membership = EventMembership.objects.create(user=judge, event=event, role=Role.JUDGE)
    projects = [Project.objects.create(team=make_team(event), name=f"P{i}", status=Status.SUBMITTED,
                                       submitted_at=timezone.now()) for i in range(3)]
    criterion = Criterion.objects.create(event=event, key="impact", label="Impact", weight=Decimal("1"))
    return event, membership, projects, criterion


def score(membership, project, criterion, submitted):
    s = Score.objects.create(judge=membership, project=project,
                             submitted_at=timezone.now() if submitted else None)
    ScoreItem.objects.create(score=s, criterion=criterion, value=Decimal("4"))
    return s


def test_a_judge_with_a_submitted_review_cannot_be_removed(judged):
    event, membership, projects, criterion = judged
    score(membership, projects[0], criterion, submitted=True)
    score(membership, projects[1], criterion, submitted=True)
    score(membership, projects[2], criterion, submitted=False)
    with pytest.raises(JudgeHasReviews) as caught:
        remove_judge(None, event, membership)
    assert (caught.value.status, caught.value.code) == (409, "judge_has_reviews")
    assert "2 submitted reviews" in str(caught.value)
    assert EventMembership.objects.filter(pk=membership.pk).exists()
    assert Score.objects.filter(judge=membership).count() == 3 and ScoreItem.objects.count() == 3
    entry = AuditLog.objects.get(action=AuditAction.JUDGE_REMOVE_REFUSED)
    assert entry.subject == event.slug and entry.detail["submitted_reviews"] == 2


def test_a_judge_with_only_drafts_and_assignments_is_removed_and_the_row_counts_them(judged):
    event, membership, projects, criterion = judged
    score(membership, projects[0], criterion, submitted=False)
    for p in projects:
        Assignment.objects.create(judge=membership, project=p, source=AssignmentSource.MANUAL)
    remove_judge(None, event, membership)
    assert not EventMembership.objects.filter(pk=membership.pk).exists()
    assert not Score.objects.exists() and not ScoreItem.objects.exists() and not Assignment.objects.exists()
    entry = AuditLog.objects.get(action=AuditAction.JUDGE_REMOVED)
    assert entry.detail["drafts_deleted"] == 1 and entry.detail["assignments_deleted"] == 3


def test_an_orm_delete_of_a_membership_with_a_submitted_score_is_refused(judged):
    _, membership, projects, criterion = judged
    score(membership, projects[0], criterion, submitted=True)
    with pytest.raises(RestrictedError), transaction.atomic():
        membership.delete()
    assert Score.objects.count() == 1


def test_deleting_the_judges_account_is_refused_too(judged):
    _, membership, projects, criterion = judged
    score(membership, projects[0], criterion, submitted=False)  # even a draft: nothing goes silently
    with pytest.raises(RestrictedError), transaction.atomic():
        membership.user.delete()
    assert Score.objects.count() == 1


def test_the_event_page_shows_the_refusal(judged, client_for):
    event, membership, projects, criterion = judged
    score(membership, projects[0], criterion, submitted=True)
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/judges/{membership.pk}/remove", follow=True)
    text = response.content.decode()
    assert "cannot be removed" in text and "judge removed." not in text
    assert EventMembership.objects.filter(pk=membership.pk).exists()


def test_deleting_the_whole_event_still_takes_its_reviews(judged):
    """The one delete RESTRICT allows: the reviews go through their projects in the same delete."""
    from core.deadlines import deadline_bypass
    event, membership, projects, criterion = judged
    score(membership, projects[0], criterion, submitted=True)
    with deadline_bypass(None, "test: delete event"):
        event.delete()
    assert not Score.objects.exists() and not EventMembership.objects.filter(pk=membership.pk).exists()
