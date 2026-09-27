"""Deadline enforcement: one rule, enforced twice.

The rule
--------
A participant write (anything that changes a team, a membership, a project, its answers,
tags or images) is allowed only while

    now < effective close

where *effective close* is the event's `submissions_close_at`, pushed later for one team if an
organizer granted that team an extension. The window is half-open, so **the close instant
itself is already closed**. Starting a project additionally needs `now >= submissions_open_at`;
teams may form as soon as the event is published.

"now" is the **database's** clock (`statement_timestamp()`), not the web worker's, so every
gunicorn worker -- and the trigger below -- agree on what time it is.

Enforced twice
--------------
1. `check_submission_window()` -- called first in every participant service function and at
   the top of every write view, before permission checks and validation, so a late write is
   refused *as late* (HTTP 409 `submissions_closed`), never disguised as a 403 or a 400.
   Refusals are written to the audit log with how late they were.
2. A Postgres trigger (projects/migrations/0002_deadline_trigger.py) on every table a
   participant can write. Even a code path that forgot step 1, a raw SQL statement, or a request
   that passed step 1 at 23:29:59.9 and reached the database at 23:30:00.1 is refused.

Organizers sometimes must write after the close (a judging decision, a fix a team asked for).
They do it inside `deadline_bypass(request, reason)`, which is audited.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

TRIGGER_MARKER = "dogfood_submissions_closed"


class SubmissionsClosed(Exception):
    def __init__(self, event=None, closed_at=None, now=None):
        self.event, self.closed_at, self.now = event, closed_at, now
        super().__init__("Submissions are closed.")

    @property
    def late_by_seconds(self):
        if self.closed_at and self.now:
            return max(0, int((self.now - self.closed_at).total_seconds()))
        return None


class SubmissionsNotOpen(Exception):
    def __init__(self, event, opens_at):
        self.event, self.opens_at = event, opens_at
        super().__init__("Submissions are not open yet.")


def db_now():
    """The database's clock. Falls back to the app clock on SQLite (local development only)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT statement_timestamp()")
            return cursor.fetchone()[0]
    return timezone.now()


def is_closed(now, closes):
    """Half-open window: at the close instant it is already closed."""
    return now >= closes


def effective_close(event, team=None):
    """(close time for this team, whether an extension moved it)."""
    closes = event.submissions_close_at
    if team is not None and team.pk:
        from teams.models import TeamExtension

        until = TeamExtension.objects.filter(team=team).values_list("until", flat=True).first()
        if until and until > closes:
            return until, True
    return closes, False


@dataclass
class Window:
    now: datetime
    opens_at: datetime
    closes_at: datetime
    extended: bool

    @property
    def is_closed(self):
        return is_closed(self.now, self.closes_at)

    @property
    def not_yet_open(self):
        return self.now < self.opens_at

    @property
    def is_open(self):
        return not self.is_closed and not self.not_yet_open


def window(event, team=None):
    closes, extended = effective_close(event, team)
    return Window(now=db_now(), opens_at=event.submissions_open_at, closes_at=closes, extended=extended)


def check_submission_window(request, event, team=None, *, action, needs_open=False):
    """Raise unless a participant may write to `event` (for `team`) right now.

    `needs_open`: also refuse before submissions open (starting a project does; forming a team
    does not).
    """
    from core import audit
    from core.models import AuditAction

    state = window(event, team)
    if state.is_closed:
        error = SubmissionsClosed(event, state.closes_at, state.now)
        audit.record(
            AuditAction.LATE_WRITE_REFUSED, request=request, subject=event.slug,
            attempted=action, team=getattr(team, "name", ""),
            closed_at=state.closes_at.isoformat(), late_by_seconds=error.late_by_seconds,
        )
        raise error
    if needs_open and state.not_yet_open:
        audit.record(
            AuditAction.EARLY_WRITE_REFUSED, request=request, subject=event.slug,
            attempted=action, opens_at=state.opens_at.isoformat(),
        )
        raise SubmissionsNotOpen(event, state.opens_at)
    return state


@contextmanager
def deadline_bypass(request, reason, *, subject=""):
    """Let organizer code write after the close. Audited, and scoped to one transaction:
    `SET LOCAL` ends with it, so the bypass cannot leak into the next request."""
    from core import audit
    from core.models import AuditAction

    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL dogfood.deadline_bypass = 'on'")
        try:
            yield
        finally:
            if connection.vendor == "postgresql" and not connection.needs_rollback:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL dogfood.deadline_bypass = 'off'")
    if request is not None:
        audit.record(AuditAction.DEADLINE_BYPASSED, request=request, subject=subject, reason=reason)


def trigger_refusal(error):
    """If `error` is the trigger's refusal, a SubmissionsClosed describing it; else None."""
    if isinstance(error, DatabaseError) and TRIGGER_MARKER in str(error):
        closed_at = None
        text = str(error)
        if "closed at " in text:
            stamp = text.split("closed at ", 1)[1].split()[0].strip()
            try:
                closed_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                closed_at = None
        return SubmissionsClosed(None, closed_at, timezone.now())
    return None
