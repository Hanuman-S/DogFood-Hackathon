"""Boundary tests for the deadline guard itself.

These test `core.deadlines` in isolation, against a stand-in object rather than a real Event,
because the guard is duck-typed on purpose. The end-to-end tests that drive real URLs and
real events come later; this file pins down the arithmetic so that a failure there is
unambiguous about which layer broke.

The boundary that matters most: **the close instant is refused.** "Closes at 18:00" means the
last accepted write is at 17:59:59.
"""

import dataclasses
import datetime as dt

import pytest

from core import clock
from core.deadlines import assert_submissions_open, submissions_are_open
from core.errors import SubmissionsClosed, SubmissionsNotOpen

UTC = dt.timezone.utc

OPEN_AT = dt.datetime(2026, 2, 26, 18, 0, tzinfo=UTC)
CLOSE_AT = dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)


@dataclasses.dataclass
class FakeEvent:
    """Minimal stand-in satisfying the HasSubmissionWindow protocol."""

    submissions_open_at: dt.datetime = OPEN_AT
    submissions_close_at: dt.datetime = CLOSE_AT
    name: str = "Sample Hack 2026"


@pytest.fixture
def event():
    return FakeEvent()


# --------------------------------------------------------------------------------------
# the four boundary instants the brief calls out
# --------------------------------------------------------------------------------------


def test_exactly_at_the_close_instant_is_refused(event):
    with clock.frozen_at(CLOSE_AT):
        with pytest.raises(SubmissionsClosed):
            assert_submissions_open(event)


def test_one_second_before_close_is_allowed(event):
    with clock.frozen_at(CLOSE_AT - dt.timedelta(seconds=1)):
        assert_submissions_open(event)  # does not raise


def test_one_second_after_close_is_refused(event):
    with clock.frozen_at(CLOSE_AT + dt.timedelta(seconds=1)):
        with pytest.raises(SubmissionsClosed):
            assert_submissions_open(event)


def test_before_the_window_opens_is_refused(event):
    with clock.frozen_at(OPEN_AT - dt.timedelta(seconds=1)):
        with pytest.raises(SubmissionsNotOpen):
            assert_submissions_open(event)


def test_exactly_at_the_open_instant_is_allowed(event):
    """The window is half-open, `[open, close)`: the opening instant is inside it."""
    with clock.frozen_at(OPEN_AT):
        assert_submissions_open(event)


# --------------------------------------------------------------------------------------
# what the refusal carries
# --------------------------------------------------------------------------------------


def test_closed_error_carries_the_code_and_closing_instant(event):
    """The API contract is `{"error": "submissions_closed", "closed_at": "<ISO UTC>"}`."""
    with clock.frozen_at(CLOSE_AT):
        with pytest.raises(SubmissionsClosed) as caught:
            assert_submissions_open(event)

    error = caught.value
    payload = error.as_dict()
    assert payload["error"] == "submissions_closed"
    assert payload["closed_at"] == "2026-03-01T18:00:00Z"
    assert error.status_code == 409
    # The human-readable half names the event and the instant, in UTC.
    assert "Sample Hack 2026" in payload["detail"]
    assert "2026-03-01T18:00:00Z" in payload["detail"]


def test_not_open_yet_reuses_the_same_error_code(event):
    """One code for both edges of the window, per the brief.

    An API client should not have to handle two spellings of "not now". The message and the
    `opens_at` field distinguish the cases for a human.
    """
    with clock.frozen_at(OPEN_AT - dt.timedelta(hours=1)):
        with pytest.raises(SubmissionsNotOpen) as caught:
            assert_submissions_open(event)

    payload = caught.value.as_dict()
    assert payload["error"] == "submissions_closed"
    assert caught.value.status_code == 409
    assert payload["opens_at"] == "2026-02-26T18:00:00Z"
    assert "open at" in payload["detail"]


def test_not_open_is_catchable_as_submissions_closed(event):
    """Callers that only care "can I write?" catch one exception, not two."""
    with clock.frozen_at(OPEN_AT - dt.timedelta(hours=1)):
        with pytest.raises(SubmissionsClosed):
            assert_submissions_open(event)


# --------------------------------------------------------------------------------------
# the non-raising form used by templates
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment,expected",
    [
        (OPEN_AT - dt.timedelta(seconds=1), False),
        (OPEN_AT, True),
        (CLOSE_AT - dt.timedelta(seconds=1), True),
        (CLOSE_AT, False),
        (CLOSE_AT + dt.timedelta(seconds=1), False),
    ],
)
def test_submissions_are_open_agrees_with_the_guard(event, moment, expected):
    """`submissions_are_open` decides what the UI offers; it must never disagree with what
    the guard enforces, or a participant sees a button that refuses them."""
    with clock.frozen_at(moment):
        assert submissions_are_open(event) is expected

        raised = False
        try:
            assert_submissions_open(event)
        except SubmissionsClosed:
            raised = True
        assert raised is (not expected)


def test_the_fixture_event_is_closed_at_real_current_time():
    """A sanity check on the premise of the acceptance checker's third T1 check.

    The organizer fixture closes 2026-03-01T18:00Z. An honestly seeded portal running now is
    therefore already closed, with no clock manipulation involved anywhere.
    """
    event = FakeEvent()
    assert clock.now() > event.submissions_close_at
    with pytest.raises(SubmissionsClosed):
        assert_submissions_open(event)
