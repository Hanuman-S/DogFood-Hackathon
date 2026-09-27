"""The portal's single source of time.

Every deadline decision in this codebase is made against `core.clock.now()`. Nothing calls
`datetime.now()`, `datetime.utcnow()` or `django.utils.timezone.now()` directly -- if it
did, that call site would be untestable without freezing the process clock globally, and
"the deadline actually holds" is the one T1 behaviour we most need to prove.

Injecting a clock rather than freezing time has a second benefit: a test can run the real
clock and a fake clock inside the same test process, so a boundary test ("one second before
close is allowed, the close instant is refused") does not have to trust that a global patch
was torn down correctly.

Usage in application code::

    from core import clock

    if clock.now() >= event.submissions_close_at:
        ...

Usage in tests::

    with clock.frozen_at("2026-03-01T17:59:59Z"):
        ...            # now() returns exactly that instant
"""

from __future__ import annotations

import datetime as _datetime
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from django.utils import timezone

UTC = _datetime.timezone.utc

# When set, this callable replaces the real clock. Module-global rather than a
# thread-local: tests are single-threaded, and a thread-local would silently fail to apply
# inside Django's test client.
_override: Callable[[], _datetime.datetime] | None = None


def now() -> _datetime.datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    Always UTC, never naive. Callers can compare the result against any stored timestamp
    without thinking about conversion, because everything in the database is UTC too.
    """
    if _override is not None:
        moment = _override()
    else:
        moment = timezone.now()
    return _as_utc(moment)


def today() -> _datetime.date:
    """The current UTC date. Used only for display; never for a deadline decision."""
    return now().date()


def _as_utc(moment: _datetime.datetime) -> _datetime.datetime:
    """Normalize to an aware UTC datetime, refusing naive input.

    A naive datetime reaching a deadline comparison is a bug worth crashing on rather than
    guessing a timezone for: guessing is how a submission window silently moves by hours.
    """
    if moment.tzinfo is None:
        raise ValueError(
            "core.clock received a naive datetime. All portal timestamps are UTC-aware; "
            f"got {moment!r}"
        )
    return moment.astimezone(UTC)


def parse_utc(value: str) -> _datetime.datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts the trailing `Z` that the organizer fixtures use, which
    `datetime.fromisoformat` only learned to handle in Python 3.11.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = _datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # A fixture timestamp without an offset is documented as UTC, so read it as UTC
        # rather than as local time.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(moment: _datetime.datetime | None) -> str | None:
    """Render an instant the way the API and the audit log record it: ISO-8601 with `Z`.

    `datetime.isoformat()` renders UTC as `+00:00`; the fixtures and the rest of the
    hackathon's tooling use `Z`, so the portal is consistent with its input.
    """
    if moment is None:
        return None
    return _as_utc(moment).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------------------
# test seams
# --------------------------------------------------------------------------------------


def set_clock(source: Callable[[], _datetime.datetime] | None) -> None:
    """Install a clock function, or pass None to restore the real one."""
    global _override
    _override = source


@contextmanager
def frozen_at(moment: _datetime.datetime | str) -> Iterator[_datetime.datetime]:
    """Freeze `now()` at a single instant for the duration of the block.

    Accepts a datetime or an ISO-8601 string so that boundary tests can be written in the
    same notation the fixtures use.
    """
    fixed = parse_utc(moment) if isinstance(moment, str) else _as_utc(moment)
    previous = _override
    set_clock(lambda: fixed)
    try:
        yield fixed
    finally:
        set_clock(previous)


@contextmanager
def offset_by(delta: _datetime.timedelta) -> Iterator[None]:
    """Shift `now()` by a fixed offset while still advancing normally."""
    previous = _override
    base = previous if previous is not None else timezone.now
    set_clock(lambda: _as_utc(base()) + delta)
    try:
        yield
    finally:
        set_clock(previous)
