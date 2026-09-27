"""The single write path into the audit log."""

from core.models import AuditLog
from core.net import client_ip, user_agent


def record(action, *, request=None, actor=None, subject="", **detail):
    """Append one audit row.

    `actor` defaults to the request's authenticated user. Called outside any transaction that
    might roll back: a refused action must leave its trace even though the action did not
    happen.
    """
    if actor is None and request is not None:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            actor = user
    return AuditLog.objects.create(
        action=action,
        actor=actor,
        actor_email=getattr(actor, "email", "") or "",
        subject=(subject or "")[:254],
        ip=client_ip(request) if request is not None else None,
        user_agent=user_agent(request) if request is not None else "",
        detail=detail,
    )
