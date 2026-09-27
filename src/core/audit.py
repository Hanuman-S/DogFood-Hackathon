"""The single write path into the audit log."""

from dataclasses import dataclass

from core.models import AuditLog
from core.net import client_ip, user_agent


@dataclass(frozen=True)
class Origin:
    """Where a write came from, taken off the request by the view. Services take this (never the
    request itself), so a service can be called from a view, a command or a test alike."""

    ip: str | None = None
    user_agent: str = ""


def origin_of(request):
    if request is None:
        return None
    return Origin(ip=client_ip(request), user_agent=user_agent(request))


def record(action, *, request=None, origin=None, actor=None, subject="", **detail):
    """Append one audit row.

    `actor` defaults to the request's authenticated user. `origin` (from `audit.origin_of(request)`)
    stands in for the request in services that do not take one. Called outside any transaction
    that might roll back: a refused action must leave its trace even though the action did not
    happen.
    """
    if actor is None and request is not None:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            actor = user
    if origin is None and request is not None:
        origin = origin_of(request)
    return AuditLog.objects.create(
        action=action,
        actor=actor,
        actor_email=getattr(actor, "email", "") or "",
        subject=(subject or "")[:254],
        ip=origin.ip if origin is not None else None,
        user_agent=origin.user_agent if origin is not None else "",
        detail=detail,
    )
