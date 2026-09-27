"""The judging window (core/judging.py) and extending it (events.services.extend_judging)."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from _dates import dt_fields
from accounts.roles import Role
from core.judging import JudgingNotOpen, JudgingWindow, check_judging_window, refusal_response
from core.models import AuditAction, AuditLog
from events.models import Event

pytestmark = pytest.mark.django_db


class FakeRequest:
    def __init__(self, user=None):
        self.user = user
        self.META = {"REMOTE_ADDR": "10.0.0.1"}
        self.headers = {}


def set_dates(event, **dates):
    Event.objects.filter(pk=event.pk).update(**dates)
    event.refresh_from_db()
    return event


def in_judging(event, **extra):
    now = timezone.now()
    return set_dates(
        event, starts_at=now - timedelta(days=4, hours=1), submissions_open_at=now - timedelta(days=4),
        submissions_close_at=now - timedelta(days=2), judging_starts_at=now - timedelta(days=1),
        judging_ends_at=now + timedelta(days=1), **extra,
    )


def after_judging(event):
    now = timezone.now()
    return set_dates(
        event, starts_at=now - timedelta(days=6, hours=1), submissions_open_at=now - timedelta(days=6),
        submissions_close_at=now - timedelta(days=4), judging_starts_at=now - timedelta(days=3),
        judging_ends_at=now - timedelta(hours=1),
    )


def test_reviews_may_be_written_only_inside_the_window(make_event):
    event = make_event()  # submissions still open: judging has not started
    with pytest.raises(JudgingNotOpen) as not_yet:
        check_judging_window(FakeRequest(), event, action="save review")
    assert not_yet.value.code == "judging_not_started"

    check_judging_window(FakeRequest(), in_judging(event), action="save review")

    with pytest.raises(JudgingNotOpen) as late:
        check_judging_window(FakeRequest(), after_judging(event), action="submit review")
    assert late.value.code == "judging_closed"
    refusals = AuditLog.objects.filter(action=AuditAction.JUDGING_WRITE_REFUSED)
    assert sorted(refusals.values_list("detail__reason", flat=True)) == ["judging_closed", "judging_not_started"]


def test_the_window_is_half_open_like_the_deadline():
    now = timezone.now()
    assert JudgingWindow(now=now, starts_at=now, ends_at=now + timedelta(hours=1)).is_open
    assert JudgingWindow(now=now, starts_at=now - timedelta(hours=1), ends_at=now).is_closed


def test_a_refusal_becomes_a_409_that_says_when(make_event):
    event = after_judging(make_event())
    with pytest.raises(JudgingNotOpen) as error:
        check_judging_window(FakeRequest(), event, action="submit review")
    response = refusal_response(error.value)
    body = json.loads(response.content)
    assert response.status_code == 409 and body["error"] == "judging_closed" and body["closed_at"].endswith("Z")


# --- extending judging ------------------------------------------------------------------------------


def extend(client, event, new_end, reason="two judges fell ill"):
    return client.post(f"/organizer/events/{event.slug}/judging/extend",
                       {**dt_fields("new_end", new_end), "reason": reason})


def test_extending_reopens_judging_keeps_the_original_and_is_audited(make_event, client_for):
    event = after_judging(make_event())
    original = event.judging_ends_at
    new_end = (timezone.now() + timedelta(days=2)).replace(second=0, microsecond=0)
    assert extend(client_for(event.organizer), event, new_end).status_code == 302
    event.refresh_from_db()
    assert event.judging_ends_at == new_end and event.original_judging_ends_at == original
    check_judging_window(FakeRequest(), event, action="save review")  # open again
    entry = AuditLog.objects.get(action=AuditAction.JUDGING_EXTENDED)
    assert entry.detail["reason"] == "two judges fell ill"


def test_results_move_with_judging_when_they_would_fall_inside_it(make_event, client_for):
    event = in_judging(make_event(), results_at=timezone.now() + timedelta(days=1, hours=2))
    old_end, old_results = event.judging_ends_at, event.results_at
    new_end = (old_results + timedelta(days=1)).replace(second=0, microsecond=0)
    assert extend(client_for(event.organizer), event, new_end).status_code == 302
    event.refresh_from_db()
    assert event.judging_ends_at == new_end
    assert event.results_at == old_results + (new_end - old_end)  # same shift, order kept


@pytest.mark.parametrize("offset, message", [
    (timedelta(hours=-2), b"must move the judging end later"),
])
def test_an_extension_must_move_the_end_later(make_event, client_for, offset, message):
    event = in_judging(make_event())
    response = extend(client_for(event.organizer), event, event.judging_ends_at + offset)
    assert response.status_code == 400 and message in response.content


def test_only_this_events_organizers_extend_judging(make_event, make_user, client_for):
    event = after_judging(make_event())
    for user, status in [(make_user(role=Role.ORGANIZER), 404), (make_user(role=Role.JUDGE), 403)]:
        assert extend(client_for(user), event, timezone.now() + timedelta(days=2)).status_code == status
    assert not AuditLog.objects.filter(action=AuditAction.JUDGING_EXTENDED).exists()
