"""Guards that service functions call, and that record what they refused.

`core.deadlines.assert_submissions_open` is the decision; this module is the decision *plus* the
audit entry. Service functions call `guard_submissions_open`, so the brief's requirement that
"every refused write due to the deadline" is logged holds automatically rather than depending on
each call site remembering.

**Why the guard runs before any transaction.** The audit row recording a refusal must survive, and
a row written inside a transaction that then rolls back does not. Every service function therefore
calls the guard *first*, before opening `transaction.atomic()`, so the refusal is committed
independently of the write it refused. The alternative -- logging from the view's exception handler
-- spreads one rule across two layers and is easy to forget on a new endpoint.
"""

from __future__ import annotations

from core import audit
from core.deadlines import assert_submissions_open
from core.errors import PermissionDenied, SubmissionsClosed


def guard_submissions_open(
    event,
    *,
    actor=None,
    action: str,
    target=None,
    request=None,
    now=None,
) -> None:
    """Refuse a participant write outside the submission window, and record the refusal.

    Args:
        action: a short description of what was attempted, e.g. `"create_team"`. It lands in the
            audit row's metadata, which is what makes the trail readable: "refused: deadline" on
            its own does not tell an organizer what the participant was trying to do.
        now: the instant to judge against, passed through to `assert_submissions_open`. A
            service that also stamps a timestamp on the row reads the clock once and passes
            the same value to both, so the two can never disagree.

    Raises:
        SubmissionsClosed: including its `SubmissionsNotOpen` subclass.
    """
    try:
        assert_submissions_open(event, now=now)
    except SubmissionsClosed as exc:
        audit.record_refusal(
            reason=exc.code,
            actor=actor,
            event=event,
            target=target,
            metadata={
                "attempted": action,
                "closed_at": exc.extra.get("closed_at"),
                "opens_at": exc.extra.get("opens_at"),
            },
            request=request,
        )
        raise


def guard_permission(
    allowed: bool,
    *,
    actor=None,
    action: str,
    event=None,
    target=None,
    request=None,
    message: str | None = None,
) -> None:
    """Refuse an action the caller is not entitled to, and record the refusal.

    For cases where the resource's existence is not a secret. When it *is* a secret -- another
    team's draft -- the view raises `Http404` instead, and there is deliberately no audit entry: a
    404 for a resource the caller may not know about is not an interesting security event, and
    logging one per probe would drown the trail an organizer needs to read.
    """
    if allowed:
        return

    audit.record_refusal(
        reason="permission_denied",
        actor=actor,
        event=event,
        target=target,
        metadata={"attempted": action},
        request=request,
    )
    raise PermissionDenied(message or PermissionDenied.message)
