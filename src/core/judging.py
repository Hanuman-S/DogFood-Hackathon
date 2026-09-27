"""The judging window: reviews may be written only between `judging_starts_at` and
`judging_ends_at`, by the database's clock (like the submission deadline in core/deadlines.py).

For the judge side: call `check_judging_window(request, event, action=...)` **first** in every
review write (save a draft, submit, reopen), before permission checks and validation, so a late
write is refused as late (HTTP 409 `judging_closed`) rather than disguised as a 403 or a 400.
`refusal_response()` turns the exception into that response for JSON clients. Refusals are
written to the audit log.

The window is half-open, like the deadline: at `judging_ends_at` itself it is already closed.
Organizers move the end with `events.services.extend_judging` (audited).
"""

from dataclasses import dataclass
from datetime import datetime

from core.deadlines import db_now


def judging_closed(event, now) -> bool:
    """THE rule for "judging is over": `now` (the database clock) has reached `judging_ends_at`.
    The boundary instant counts as closed, like the submission window. Everything that asks --
    the judges' write window, the assignment freeze, the results gate
    (scoring.services.compute_snapshot) -- asks this function; nothing else compares against
    `judging_ends_at`. An extension moves `judging_ends_at` itself (events.services.extend_judging)."""
    return now >= event.judging_ends_at


class JudgingNotOpen(Exception):
    """Refused: judging has not started, or has ended."""

    def __init__(self, event, code, when, now):
        self.event, self.code, self.when, self.now = event, code, when, now
        super().__init__(
            "Judging has not started yet." if code == "judging_not_started" else "Judging is closed."
        )


@dataclass
class JudgingWindow:
    now: datetime
    starts_at: datetime
    ends_at: datetime

    @property
    def not_started(self):
        return self.now < self.starts_at

    @property
    def is_closed(self):
        return self.now >= self.ends_at  # the same rule as judging_closed(), on this snapshot of the dates

    @property
    def is_open(self):
        return not self.not_started and not self.is_closed


def judging_window(event):
    return JudgingWindow(now=db_now(), starts_at=event.judging_starts_at, ends_at=event.judging_ends_at)


def check_judging_window(request, event, *, action):
    """Raise `JudgingNotOpen` (and log it) unless reviews may be written for `event` now."""
    from core import audit
    from core.models import AuditAction

    state = judging_window(event)
    if state.is_open:
        return state
    code, when = (
        ("judging_not_started", state.starts_at) if state.not_started else ("judging_closed", state.ends_at)
    )
    audit.record(
        AuditAction.JUDGING_WRITE_REFUSED, request=request, subject=event.slug,
        attempted=action, reason=code, boundary=when.isoformat(),
    )
    raise JudgingNotOpen(event, code, when, state.now)


def refusal_response(error):
    """The 409 a JSON client gets for a write outside the judging window."""
    from django.http import JsonResponse

    key = "starts_at" if error.code == "judging_not_started" else "closed_at"
    return JsonResponse(
        {"error": error.code, "detail": str(error), key: error.when.isoformat().replace("+00:00", "Z")},
        status=409,
    )
