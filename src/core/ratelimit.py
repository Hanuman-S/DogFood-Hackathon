"""Rate limits counted in Postgres, from the audit log -- the login throttle's pattern
(accounts/throttle.py), for any write that leaves an audit row per attempt.

Counting rows rather than keeping counters in memory means a limit survives a restart and is shared
by every gunicorn worker. Two limits per write: per actor (whatever identifies them: an account, a
voter link, an open-link cookie) and per IP (a keyed hash; no address is stored). The window is
sliding and read on the database clock. Rows recording a throttle are not counted, so being refused
does not extend the refusal. Nor are rows an event bundle imported (AuditLog.objects.live()): they
describe another install.
"""

from dataclasses import dataclass
from datetime import timedelta

from core.models import AuditLog


@dataclass(frozen=True)
class Limit:
    count: int
    window: timedelta


def exceeded(actions, *, now, per_actor=None, actor_filter=None, per_ip=None, ip_hash=""):
    """"actor" or "ip" -- whichever limit this attempt would break -- or "" if neither."""
    if per_actor is not None and actor_filter is not None:
        n = AuditLog.objects.live().filter(action__in=actions, created_at__gte=now - per_actor.window, **actor_filter).count()
        if n >= per_actor.count:
            return "actor"
    if per_ip is not None and ip_hash:
        n = AuditLog.objects.live().filter(action__in=actions, ip_hash=ip_hash, created_at__gte=now - per_ip.window).count()
        if n >= per_ip.count:
            return "ip"
    return ""
