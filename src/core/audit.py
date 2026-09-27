"""Writing audit entries.

One function, called from service functions. Views do not write audit rows directly, for the
same reason they do not write anything else directly: the UI and the API must produce the same
trail for the same action.

Recording must never break the action it describes -- with one deliberate exception, noted on
`record_refusal`.
"""

from __future__ import annotations

import logging

from core.models import AuditAction, AuditLog

logger = logging.getLogger(__name__)


def record(
    action: str,
    *,
    actor=None,
    event=None,
    target=None,
    metadata: dict | None = None,
    request=None,
) -> AuditLog | None:
    """Append one audit entry.

    `target` is any model instance; its class name and primary key are stored as strings so
    the row outlives the object. `request`, when given, contributes the client IP and user
    agent -- which is what makes the login-throttle counting possible.

    Failures here are logged and swallowed. An audit write that fails must not roll back a
    legitimate submission five minutes before a deadline; losing the row is bad, losing the
    participant's work is worse.
    """
    payload = dict(metadata or {})

    if request is not None:
        payload.setdefault("ip", client_ip(request))
        agent = request.META.get("HTTP_USER_AGENT", "")
        if agent:
            payload.setdefault("user_agent", agent[:200])

    # An unsaved or anonymous user cannot be a foreign key target.
    if actor is not None and (not getattr(actor, "is_authenticated", False) or actor.pk is None):
        actor = None

    try:
        return AuditLog.objects.create(
            actor=actor,
            event=event,
            action=action,
            target_type=type(target).__name__ if target is not None else "",
            target_id=str(target.pk) if target is not None and target.pk else "",
            metadata=payload,
        )
    except Exception:  # noqa: BLE001 - see docstring: never break the caller
        logger.exception("failed to write audit entry for action=%s", action)
        return None


def record_refusal(
    *,
    reason: str,
    actor=None,
    event=None,
    target=None,
    metadata: dict | None = None,
    request=None,
) -> AuditLog | None:
    """Record a write refused by the deadline guard or the permission layer.

    Called from the exception path, so it runs *after* the failed write. It must be called
    outside any transaction that is about to roll back, or the evidence rolls back with it --
    the service layer therefore raises, and the view records. That ordering is the reason this
    is a separate function rather than an argument to `record`.
    """
    payload = dict(metadata or {})
    payload["reason"] = reason
    action = (
        AuditAction.REFUSED_DEADLINE
        if reason.startswith("submissions_")
        else AuditAction.REFUSED_PERMISSION
    )
    return record(
        action, actor=actor, event=event, target=target, metadata=payload, request=request
    )


def client_ip(request) -> str:
    """Best-effort client address.

    `X-Forwarded-For` is honoured only when the deployment declares a trusted proxy, because
    the header is client-supplied and trusting it unconditionally lets anyone spoof the IP the
    login throttle counts against -- turning the throttle into a way to lock *other* people
    out. Default deployment has no proxy, so REMOTE_ADDR is used.
    """
    from django.conf import settings

    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            # Left-most entry is the original client, as appended by the first proxy.
            return forwarded.split(",")[0].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "")[:45]
