"""Comments on gallery projects (T3), at the service layer: who may post, the order of the checks,
rate limits, duplicates, deleting, moderation, and the read rule. Pages and the JSON API are tested
in test_comments.py; they call these same services."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from accounts.roles import ADMIN, Role
from core.audit import Origin
from core.models import AuditAction, AuditLog
from projects import comments
from projects.comment_errors import (
    CommentsDisabled, DuplicateComment, InvalidComment, InvalidModeration, LoginRequired, NoComment, NoEvent,
    NotCommentable, RateLimited,
)
from projects.models import Comment, Project, Status

pytestmark = pytest.mark.django_db

HERE = Origin(ip_hash="a" * 64, user_agent="pytest")


@pytest.fixture
def gallery_project(make_event, make_team):
    """A submitted project of a published event: in the anonymous gallery."""
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, name="Lamp Post", status=Status.SUBMITTED, submitted_at=timezone.now())
    project.event = event  # the instance carrying the test's `organizer`
    project.captain = team.captain
    return project


@pytest.fixture
def commenter(make_user):
    return make_user(email="reader@example.org")


def post(project, author, body="Nice work", origin=HERE):
    return comments.post_comment(project.pk, body, author=author, origin=origin)


def codes(error_class):
    return error_class.status, error_class.code


# --- posting ---------------------------------------------------------------------------------------

def test_a_logged_in_account_posts_and_it_is_audited(gallery_project, commenter):
    comment = post(gallery_project, commenter, "  Nice **work**  ")
    assert comment.body == "Nice **work**"  # stored stripped, rendered later through core/markdown.py
    entry = AuditLog.objects.get(action=AuditAction.COMMENT_POSTED)
    assert entry.subject == gallery_project.event.slug and entry.actor == commenter
    assert entry.detail == {"project": str(gallery_project.pk), "comment": comment.pk}
    assert entry.ip_hash == HERE.ip_hash


def test_anonymous_cannot_post(gallery_project):
    with pytest.raises(LoginRequired) as caught:
        comments.post_comment(gallery_project.pk, "hi", author=AnonymousUser(), origin=HERE)
    assert codes(caught.value) == (401, "login_required")
    assert not Comment.objects.exists()
    assert AuditLog.objects.get(action=AuditAction.COMMENT_ANONYMOUS_REFUSED).detail["reason"] == "not logged in"
    assert not AuditLog.objects.filter(action__in=comments.COMMENT_WRITE_ACTIONS).exists()


def test_the_per_ip_default_is_generous_and_per_account_is_the_primary_control():
    from django.conf import settings
    assert settings.COMMENT_RATE_PER_IP == 200 and settings.COMMENT_RATE_PER_USER == 5
    assert settings.COMMENT_ANON_RATE_PER_IP == 60


def test_an_anonymous_flood_writes_at_most_cap_plus_one_rows(gallery_project, commenter):
    """100 anonymous attempts from one address: every one is a 401, but only the first 60 are audited,
    then one throttle row; the audit log cannot be flooded. Someone logged in behind that address
    still posts."""
    from django.conf import settings
    cap = settings.COMMENT_ANON_RATE_PER_IP
    for _ in range(100):
        with pytest.raises(LoginRequired) as caught:
            comments.post_comment(gallery_project.pk, "hi", author=AnonymousUser(), origin=HERE)
        assert caught.value.status == 401
    anonymous = AuditLog.objects.filter(ip_hash=HERE.ip_hash, action__in=(
        AuditAction.COMMENT_ANONYMOUS_REFUSED, AuditAction.COMMENT_ANONYMOUS_THROTTLED))
    assert anonymous.count() <= cap + 1
    assert anonymous.filter(action=AuditAction.COMMENT_ANONYMOUS_REFUSED).count() == cap
    assert anonymous.filter(action=AuditAction.COMMENT_ANONYMOUS_THROTTLED).count() == 1
    assert post(gallery_project, commenter).pk


@override_settings(COMMENT_ANON_RATE_PER_IP=2)
def test_anonymous_auditing_resumes_when_the_window_slides(gallery_project, monkeypatch):
    for _ in range(5):
        with pytest.raises(LoginRequired):
            comments.post_comment(gallery_project.pk, "hi", author=AnonymousUser(), origin=HERE)
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_ANONYMOUS_REFUSED).count() == 2
    later = timezone.now() + timedelta(minutes=11)
    monkeypatch.setattr("projects.comments.db_now", lambda: later)
    monkeypatch.setattr("core.audit.AuditLog.objects.create", _create_at(later))
    with pytest.raises(LoginRequired):
        comments.post_comment(gallery_project.pk, "hi", author=AnonymousUser(), origin=HERE)
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_ANONYMOUS_REFUSED).count() == 3


def _create_at(when):
    """AuditLog.objects.create, stamping rows at `when` (the audit log uses the app clock)."""
    original = AuditLog.objects.create

    def create(**fields):
        fields.setdefault("created_at", when)
        return original(**fields)
    return create


@override_settings(COMMENT_RATE_PER_IP=10)
def test_anonymous_attempts_do_not_use_up_the_per_ip_limit(gallery_project, commenter):
    """50 anonymous attempts from one address (five times the per-IP limit), then someone logged in
    behind the same address posts: anonymous refusals are audited in a bucket of their own."""
    for _ in range(50):
        with pytest.raises(LoginRequired):
            comments.post_comment(gallery_project.pk, "hi", author=AnonymousUser(), origin=HERE)
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_ANONYMOUS_REFUSED, ip_hash=HERE.ip_hash).count() == 50
    assert post(gallery_project, commenter).pk
    assert not AuditLog.objects.filter(action=AuditAction.COMMENT_THROTTLED).exists()


def test_a_draft_cannot_be_commented_on_not_even_by_its_own_team(make_event, make_team):
    event = make_event()
    team = make_team(event)
    draft = Project.objects.create(team=team, name="Draft", status=Status.DRAFT)
    with pytest.raises(NotCommentable) as caught:
        comments.post_comment(draft.pk, "mine", author=team.captain, origin=HERE)
    assert codes(caught.value) == (404, "no_project")
    assert not Comment.objects.exists()
    refused = AuditLog.objects.get(action=AuditAction.COMMENT_REFUSED)
    assert refused.subject == event.slug and refused.detail["reason"] == "no project"


def test_a_project_of_an_unpublished_event_cannot_be_commented_on(make_event, make_team, commenter):
    event = make_event(published=False)
    project = Project.objects.create(team=make_team(event), name="Hidden", status=Status.SUBMITTED,
                                     submitted_at=timezone.now())
    with pytest.raises(NotCommentable):
        post(project, commenter)
    # ...not even by the event's own organizer: comments live on the public gallery only.
    with pytest.raises(NotCommentable):
        post(project, event.organizer)


@pytest.mark.parametrize("project_id", [999999, "abc", None, "1; DROP TABLE"])
def test_no_such_project_is_the_same_404(commenter, project_id):
    with pytest.raises(NotCommentable):
        comments.post_comment(project_id, "hi", author=commenter, origin=HERE)


def test_comments_turned_off_is_409_and_checked_before_the_body(gallery_project, commenter):
    event = gallery_project.event
    comments.set_comments_enabled(event, False, actor=event.organizer, origin=HERE)
    with pytest.raises(CommentsDisabled) as caught:
        post(gallery_project, commenter, "")  # an empty body too: the event rule answers first
    assert codes(caught.value) == (409, "comments_disabled")
    assert not Comment.objects.exists()


@pytest.mark.parametrize("body", ["", "   \n\t ", None, 42, ["x"]])
def test_an_empty_or_non_text_body_is_400(gallery_project, commenter, body):
    with pytest.raises(InvalidComment) as caught:
        post(gallery_project, commenter, body)
    assert codes(caught.value) == (400, "invalid_comment")


def test_the_body_limit_is_2000_characters(gallery_project, commenter):
    assert post(gallery_project, commenter, "x" * 2000).body == "x" * 2000
    with pytest.raises(InvalidComment):
        post(gallery_project, commenter, "y" * 2001)
    assert Comment.objects.count() == 1


def test_the_database_refuses_an_over_long_or_empty_body(gallery_project, commenter):
    for body in ("z" * 2001, ""):
        with pytest.raises(IntegrityError), transaction.atomic():
            Comment.objects.create(project=gallery_project, author=commenter, body=body)


def test_the_database_keeps_the_hidden_fields_together(gallery_project, commenter):
    with pytest.raises(IntegrityError), transaction.atomic():
        Comment.objects.create(project=gallery_project, author=commenter, body="x", hidden_at=timezone.now())
    with pytest.raises(IntegrityError), transaction.atomic():
        Comment.objects.create(project=gallery_project, author=commenter, body="x", hidden_at=timezone.now(),
                               hidden_by=commenter, hide_reason="")


# --- rate limits and duplicates --------------------------------------------------------------------

@override_settings(COMMENT_RATE_PER_USER=2, COMMENT_RATE_PER_IP=100)
def test_rate_limit_per_account(gallery_project, commenter):
    post(gallery_project, commenter, "one")
    post(gallery_project, commenter, "two", origin=Origin(ip_hash="b" * 64))  # another network: same account
    with pytest.raises(RateLimited) as caught:
        post(gallery_project, commenter, "three", origin=Origin(ip_hash="c" * 64))
    assert codes(caught.value) == (429, "rate_limited")
    entry = AuditLog.objects.get(action=AuditAction.COMMENT_THROTTLED)
    assert entry.detail["limit"] == "actor" and entry.ip_hash == "c" * 64
    assert entry.subject == gallery_project.event.slug
    assert Comment.objects.count() == 2


@override_settings(COMMENT_RATE_PER_USER=100, COMMENT_RATE_PER_IP=2)
def test_rate_limit_per_ip_hash(gallery_project, make_user):
    for i in range(2):
        post(gallery_project, make_user(email=f"u{i}@example.org"), f"hello {i}")
    with pytest.raises(RateLimited):
        post(gallery_project, make_user(email="u9@example.org"), "hello 9")
    entry = AuditLog.objects.get(action=AuditAction.COMMENT_THROTTLED)
    assert entry.detail["limit"] == "ip" and entry.ip_hash == HERE.ip_hash


@override_settings(COMMENT_RATE_PER_USER=1, COMMENT_RATE_PER_IP=100)
def test_refused_posts_count_and_the_limit_answers_before_the_body(gallery_project, commenter):
    with pytest.raises(InvalidComment):
        post(gallery_project, commenter, "")  # a refused attempt is still an attempt
    with pytest.raises(RateLimited):
        post(gallery_project, commenter, "")
    # Throttle rows are not counted, so being refused does not extend the refusal.
    assert AuditLog.objects.filter(action__in=comments.COMMENT_WRITE_ACTIONS).count() == 1


def test_an_identical_body_on_the_same_project_within_ten_minutes_is_409(gallery_project, commenter):
    post(gallery_project, commenter, "Great demo!")
    with pytest.raises(DuplicateComment) as caught:
        post(gallery_project, commenter, "  Great demo!  ")  # compared after stripping
    assert codes(caught.value) == (409, "duplicate_comment")
    assert Comment.objects.count() == 1
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_REFUSED, detail__reason="duplicate").count() == 1


def test_the_same_body_on_another_project_is_accepted(gallery_project, commenter, make_team):
    """The duplicate check is per (author, project): "Great demo!" on two projects is two comments."""
    other = Project.objects.create(team=make_team(gallery_project.event), name="Other", status=Status.SUBMITTED,
                                   submitted_at=timezone.now())
    post(gallery_project, commenter, "Great demo!")
    post(other, commenter, "Great demo!")
    assert Comment.objects.filter(author=commenter, body="Great demo!").count() == 2


def test_another_account_may_post_the_same_text(gallery_project, commenter, make_user):
    post(gallery_project, commenter, "+1")
    post(gallery_project, make_user(email="other@example.org"), "+1")
    assert Comment.objects.count() == 2


def test_a_deleted_comment_still_counts_as_a_duplicate(gallery_project, commenter):
    first = post(gallery_project, commenter, "spam")
    comments.delete_own_comment(first.pk, actor=commenter, origin=HERE)
    with pytest.raises(DuplicateComment):
        post(gallery_project, commenter, "spam")


def test_the_same_text_is_allowed_again_after_ten_minutes(gallery_project, commenter, monkeypatch):
    first = post(gallery_project, commenter, "Still great")
    later = first.created_at + timedelta(minutes=10, seconds=1)
    monkeypatch.setattr("projects.comments.db_now", lambda: later)
    second = post(gallery_project, commenter, "Still great")
    assert second.created_at == later and Comment.objects.count() == 2


# --- deleting --------------------------------------------------------------------------------------

def test_the_author_deletes_their_own_softly_and_once(gallery_project, commenter):
    comment = post(gallery_project, commenter)
    comments.delete_own_comment(comment.pk, actor=commenter, origin=HERE)
    comment.refresh_from_db()
    assert comment.deleted_at is not None and Comment.objects.filter(pk=comment.pk).exists()
    comments.delete_own_comment(comment.pk, actor=commenter, origin=HERE)  # again: no change, no error
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_DELETED).count() == 1


def test_nobody_else_can_delete_it_and_the_answer_is_404(gallery_project, commenter, make_user):
    comment = post(gallery_project, commenter)
    event = gallery_project.event
    for someone in (make_user(email="x@example.org"), event.organizer, make_user(role=ADMIN)):
        with pytest.raises(NoComment) as caught:
            comments.delete_own_comment(comment.pk, actor=someone, origin=HERE)
        assert codes(caught.value) == (404, "no_comment")
    comment.refresh_from_db()
    assert comment.deleted_at is None
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_MODERATION_REFUSED, subject=event.slug).count() == 3


# --- moderation ------------------------------------------------------------------------------------

def test_an_organizer_hides_with_a_reason_and_restores(gallery_project, commenter):
    organizer = gallery_project.event.organizer
    comment = post(gallery_project, commenter)
    comments.hide_comment(comment.pk, "off-topic", actor=organizer, origin=HERE)
    comment.refresh_from_db()
    assert comment.is_hidden and comment.hidden_by == organizer and comment.hide_reason == "off-topic"
    with pytest.raises(InvalidModeration):
        comments.hide_comment(comment.pk, "again", actor=organizer, origin=HERE)
    comments.restore_comment(comment.pk, "was fine", actor=organizer, origin=HERE)
    comment.refresh_from_db()
    assert not comment.is_hidden and comment.hidden_by is None and comment.hide_reason == ""
    restored = AuditLog.objects.get(action=AuditAction.COMMENT_RESTORED)
    assert restored.detail["undid"]["hide_reason"] == "off-topic" and restored.detail["reason"] == "was fine"
    with pytest.raises(InvalidModeration) as caught:
        comments.restore_comment(comment.pk, "again", actor=organizer, origin=HERE)
    assert codes(caught.value) == (400, "invalid_moderation")


@pytest.mark.parametrize("reason", ["", "   ", None, "r" * 301])
def test_hiding_needs_a_reason(gallery_project, commenter, reason):
    comment = post(gallery_project, commenter)
    with pytest.raises(InvalidModeration):
        comments.hide_comment(comment.pk, reason, actor=gallery_project.event.organizer, origin=HERE)
    comment.refresh_from_db()
    assert not comment.is_hidden


def test_a_platform_admin_can_hide(gallery_project, commenter, make_user):
    comment = post(gallery_project, commenter)
    comments.hide_comment(comment.pk, "spam", actor=make_user(role=ADMIN), origin=HERE)
    comment.refresh_from_db()
    assert comment.is_hidden


def test_another_events_organizer_or_a_judge_cannot_hide_and_gets_404(gallery_project, commenter, make_event,
                                                                       make_user):
    comment = post(gallery_project, commenter)
    outsiders = [make_event().organizer, make_user(role=Role.JUDGE), commenter]
    for outsider in outsiders:
        with pytest.raises(NoComment) as caught:
            comments.hide_comment(comment.pk, "mine now", actor=outsider, origin=HERE)
        assert codes(caught.value) == (404, "no_comment")
    comment.refresh_from_db()
    assert not comment.is_hidden
    refusals = AuditLog.objects.filter(action=AuditAction.COMMENT_MODERATION_REFUSED)
    assert refusals.count() == len(outsiders)
    assert all("not an organizer" in r.detail["reason"] for r in refusals)


def test_a_missing_comment_is_the_same_404_for_moderation(gallery_project):
    with pytest.raises(NoComment):
        comments.hide_comment(424242, "x", actor=gallery_project.event.organizer, origin=HERE)


# --- the event switch ------------------------------------------------------------------------------

def test_only_an_organizer_of_the_event_turns_comments_off(gallery_project, make_event):
    event = gallery_project.event
    with pytest.raises(NoEvent):
        comments.set_comments_enabled(event, False, actor=make_event().organizer, origin=HERE)
    event.refresh_from_db()
    assert event.comments_enabled
    comments.set_comments_enabled(event, False, actor=event.organizer, origin=HERE)
    event.refresh_from_db()
    assert not event.comments_enabled
    comments.set_comments_enabled(event, False, actor=event.organizer, origin=HERE)  # no change: not audited
    assert AuditLog.objects.filter(action=AuditAction.COMMENTS_TOGGLED).count() == 1


def test_turning_comments_off_keeps_existing_ones_readable(gallery_project, commenter):
    post(gallery_project, commenter)
    comments.set_comments_enabled(gallery_project.event, False, actor=gallery_project.event.organizer)
    _, page, _ = comments.comments_for(gallery_project.pk, AnonymousUser())
    assert len(page) == 1


# --- reading ---------------------------------------------------------------------------------------

def test_hidden_and_deleted_are_shown_only_to_organizers_and_admins(gallery_project, commenter, make_user,
                                                                   make_team):
    event = gallery_project.event
    shown = post(gallery_project, commenter, "shown")
    hidden = post(gallery_project, commenter, "hidden")
    deleted = post(gallery_project, commenter, "deleted")
    comments.hide_comment(hidden.pk, "rude", actor=event.organizer)
    comments.delete_own_comment(deleted.pk, actor=commenter)

    participant = make_team(event).captain
    judge = make_user(role=Role.JUDGE)
    for viewer in (AnonymousUser(), None, participant, judge, commenter):  # the author too
        _, page, moderator = comments.comments_for(gallery_project.pk, viewer)
        assert [c.pk for c in page] == [shown.pk] and not moderator

    for viewer in (event.organizer, make_user(role=ADMIN)):
        _, page, moderator = comments.comments_for(gallery_project.pk, viewer)
        assert [c.pk for c in page] == [deleted.pk, hidden.pk, shown.pk] and moderator


def test_reading_a_draft_is_404_for_everyone_but_its_organizers(make_event, make_team):
    event = make_event()
    team = make_team(event)
    draft = Project.objects.create(team=team, name="Draft", status=Status.DRAFT)
    for viewer in (AnonymousUser(), team.captain):
        with pytest.raises(NotCommentable):
            comments.comments_for(draft.pk, viewer)
    _, page, moderator = comments.comments_for(draft.pk, event.organizer)
    assert moderator and len(page) == 0


def test_newest_first_and_twenty_per_page_with_bad_pages_falling_back(gallery_project, commenter):
    now = timezone.now()
    Comment.objects.bulk_create(
        Comment(project=gallery_project, author=commenter, body=f"c{i}", created_at=now + timedelta(seconds=i))
        for i in range(25)
    )
    _, first, _ = comments.comments_for(gallery_project.pk, None, 1)
    assert [c.body for c in first][:2] == ["c24", "c23"] and len(first) == 20
    _, second, _ = comments.comments_for(gallery_project.pk, None, 2)
    assert len(second) == 5 and second[-1].body == "c0"
    for bad in ("abc", -3, 999, None):
        _, page, _ = comments.comments_for(gallery_project.pk, None, bad)
        assert len(page) in (5, 20)


# Pinned: the project, the gallery check, the page count, the page (authors joined in).
READ_QUERIES = 4
# Pinned for moderators: the project, the organizer-role lookup (none for an admin, whose power is a
# column already loaded), the page count, the page (authors and whoever hid it joined in).
MODERATOR_READ_QUERIES = {"organizer": 4, "admin": 3}


@pytest.mark.parametrize("n", [3, 20])
def test_the_read_is_a_fixed_number_of_queries(gallery_project, make_user, django_assert_num_queries, n):
    Comment.objects.bulk_create(Comment(project=gallery_project, author=make_user(email=f"q{i}@example.org"),
                                        body=f"b{i}") for i in range(n))
    with django_assert_num_queries(READ_QUERIES):
        _, page, _ = comments.comments_for(gallery_project.pk, None)
        assert len([(c.author.name, c.body) for c in page]) == n


@pytest.mark.parametrize("n", [3, 20])
@pytest.mark.parametrize("who", ["organizer", "admin"])
def test_the_moderator_read_is_a_fixed_number_of_queries(gallery_project, make_user, django_assert_num_queries,
                                                          n, who):
    viewer = gallery_project.event.organizer if who == "organizer" else make_user(role=ADMIN)
    viewer = type(viewer).objects.get(pk=viewer.pk)  # a fresh instance: no cached role lookups
    Comment.objects.bulk_create(
        Comment(project=gallery_project, author=make_user(email=f"m{i}@example.org"), body=f"b{i}",
                **({"hidden_at": timezone.now(), "hidden_by": viewer, "hide_reason": "x"} if i % 2 else {}))
        for i in range(n)
    )
    with django_assert_num_queries(MODERATOR_READ_QUERIES[who]):
        _, page, moderator = comments.comments_for(gallery_project.pk, viewer)
        rows = [(c.author.name, c.body, c.hidden_by.email if c.hidden_by else "") for c in page]
    assert moderator and len(rows) == n
