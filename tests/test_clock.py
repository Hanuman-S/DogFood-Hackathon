"""Tests for the injectable clock.

The clock is the foundation every deadline test stands on, so it gets its own tests. If
`frozen_at` silently failed to apply, the deadline tests would still pass -- against the real
clock, proving nothing.
"""

import datetime as dt

import pytest

from core import clock

UTC = dt.timezone.utc


def test_now_is_utc_aware():
    moment = clock.now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == dt.timedelta(0)


def test_frozen_at_applies_and_restores():
    real_before = clock.now()

    with clock.frozen_at("2026-03-01T17:59:59Z") as frozen:
        assert clock.now() == frozen
        assert clock.now() == dt.datetime(2026, 3, 1, 17, 59, 59, tzinfo=UTC)
        # Frozen means frozen: two reads inside the block agree exactly.
        assert clock.now() == clock.now()

    # ...and the real clock is back, still moving forward.
    assert clock.now() >= real_before


def test_frozen_at_restores_even_when_the_block_raises():
    with pytest.raises(RuntimeError):
        with clock.frozen_at("2026-03-01T18:00:00Z"):
            raise RuntimeError("boom")

    assert clock.now().year != 2026 or clock.now() != dt.datetime(
        2026, 3, 1, 18, 0, 0, tzinfo=UTC
    )


def test_nested_freeze_restores_the_outer_clock():
    with clock.frozen_at("2026-01-01T00:00:00Z"):
        with clock.frozen_at("2027-01-01T00:00:00Z"):
            assert clock.now().year == 2027
        assert clock.now().year == 2026


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The trailing Z the organizer fixtures use.
        ("2026-03-01T18:00:00Z", dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)),
        ("2026-03-01T18:00:00z", dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)),
        # An explicit offset is converted to UTC rather than kept as-is.
        ("2026-03-01T19:00:00+01:00", dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)),
        # No offset at all is documented as UTC, so it is read as UTC, never as local time.
        ("2026-03-01T18:00:00", dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)),
        ("  2026-03-01T18:00:00Z  ", dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)),
    ],
)
def test_parse_utc(raw, expected):
    assert clock.parse_utc(raw) == expected


def test_iso_renders_z_not_plus_offset():
    """The fixtures and the API both use `Z`; consistency avoids a class of client bug."""
    moment = dt.datetime(2026, 3, 1, 18, 0, tzinfo=UTC)
    assert clock.iso(moment) == "2026-03-01T18:00:00Z"
    assert clock.iso(None) is None


def test_iso_converts_a_non_utc_offset_before_rendering():
    moment = dt.datetime(2026, 3, 1, 19, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    assert clock.iso(moment) == "2026-03-01T18:00:00Z"


def test_naive_datetimes_are_refused_rather_than_guessed():
    """A naive timestamp in a deadline comparison is how a submission window silently moves.

    Crashing is the correct response: there is no safe default timezone to assume.
    """
    with pytest.raises(ValueError, match="naive datetime"):
        clock.iso(dt.datetime(2026, 3, 1, 18, 0))

    clock.set_clock(lambda: dt.datetime(2026, 3, 1, 18, 0))  # naive on purpose
    with pytest.raises(ValueError, match="naive datetime"):
        clock.now()


def test_offset_by_shifts_but_keeps_advancing():
    base = clock.now()
    with clock.offset_by(dt.timedelta(days=365)):
        shifted = clock.now()
        assert shifted - base >= dt.timedelta(days=364)
    assert clock.now() - base < dt.timedelta(days=1)
