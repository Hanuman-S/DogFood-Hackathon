"""The deadline guard.

`assert_submissions_open(event)` is the only function in the portal that decides whether the
submission window is open, and every participant write path calls it. Having exactly one
implementation is the point: a second copy of this comparison somewhere in a view is how a
platform ends up accepting a submission one minute after the deadline on one code path and
refusing it on another.

**Boundary semantics, stated once.** The window is half-open: `[open, close)`.

* `now < submissions_open_at`  -> refused (`SubmissionsNotOpen`)
* `now == submissions_open_at` -> allowed
* `now <  submissions_close_at` -> allowed
* `now == submissions_close_at` -> **refused**

The close instant itself is refused. A deadline of 18:00 means the last accepted write
happens at 17:59:59, which is what participants and organizers both read "closes at 18:00"
to mean.

**Where this sits in the order of checks.** On every write:
`authenticate -> resolve event (404 if missing) -> deadline -> permission -> validation`.
The deadline is checked before permission and before the request body is looked at, so a
late submission is refused as a late submission and never masked by a validation error. The
one thing that precedes it is resolving the event, because there is no window to check
without one.

**The import bypass.** Historical fixture data is, by construction, already past its
deadline. The importer therefore does not call this module at all -- it writes through
`projects.services.import_project` and friends, which are named for it and are not reachable
from any URL. See `core.services` for the boundary and DATA-MODEL.md for the rationale.
"""

from __future__ import annotations

from typing import Protocol

from core import clock
from core.errors import SubmissionsClosed, SubmissionsNotOpen


class HasSubmissionWindow(Protocol):
    """Anything with a submission window.

    Typed as a protocol rather than importing `events.models.Event` so that this module has
    no dependency on the schema and can be unit-tested against a two-field stand-in.
    """

    submissions_open_at: object
    submissions_close_at: object


def submissions_are_open(event: HasSubmissionWindow) -> bool:
    """Non-raising form, for templates and serializers deciding what to offer.

    This is a convenience for rendering, never the enforcement. A template that hides the
    submit button is a courtesy; `assert_submissions_open` is what actually refuses.
    """
    moment = clock.now()
    return event.submissions_open_at <= moment < event.submissions_close_at


def assert_submissions_open(event: HasSubmissionWindow) -> None:
    """Raise unless the submission window is open right now.

    Raises:
        SubmissionsNotOpen: the window has not started.
        SubmissionsClosed: the window has ended (or ends exactly now).
    """
    moment = clock.now()

    if moment < event.submissions_open_at:
        raise SubmissionsNotOpen(
            "Submissions for "
            f"{getattr(event, 'name', 'this event')} open at "
            f"{clock.iso(event.submissions_open_at)}.",
            opens_at=clock.iso(event.submissions_open_at),
            closed_at=clock.iso(event.submissions_close_at),
        )

    if moment >= event.submissions_close_at:
        raise SubmissionsClosed(
            "Submissions for "
            f"{getattr(event, 'name', 'this event')} closed at "
            f"{clock.iso(event.submissions_close_at)}.",
            closed_at=clock.iso(event.submissions_close_at),
        )


def assert_judging_open(event) -> None:
    """Placeholder boundary for T2, intentionally not implemented in T1.

    T2 will need the same shape of guard for the judging window. It is declared here so the
    next session extends the one module that owns time-window decisions instead of adding a
    second one. It raises rather than returning a permissive default, because a guard that
    silently allows everything is worse than no guard at all.
    """
    raise NotImplementedError(
        "Judging-window enforcement arrives in T2; see JUDGING.md. No T1 code path calls "
        "this, and it refuses rather than defaulting to open."
    )
