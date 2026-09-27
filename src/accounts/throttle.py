"""Login throttling, counted from the audit log.

Two limits, both over a sliding window (15 minutes by default):

* per (email, IP): 5 failures. Stops password guessing against one account from one place,
  and resets as soon as that email logs in successfully from that IP.
* per IP, any email: 30 failures. Stops one machine spraying many accounts.

Counting rows in Postgres rather than keeping counters in memory means the limit survives a
restart and is shared by every gunicorn worker. The cost is two indexed COUNT queries per
login attempt, which is nothing at hackathon scale.

Deliberately *not* done: locking the account itself. A per-account lock lets anyone lock a
victim out by typing their email five times; keying on (email, IP) does not.
"""

from django.conf import settings
from django.utils import timezone

from core.models import AuditAction, AuditLog


def is_throttled(email, ip):
    now = timezone.now()
    window_start = now - settings.LOGIN_FAILURE_WINDOW

    last_success = (
        AuditLog.objects.filter(
            action=AuditAction.LOGIN_OK, subject=email, ip=ip, created_at__gte=window_start
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    since = max(window_start, last_success) if last_success else window_start

    per_account = AuditLog.objects.filter(
        action=AuditAction.LOGIN_FAILED, subject=email, ip=ip, created_at__gt=since
    ).count()
    if per_account >= settings.LOGIN_FAILURE_LIMIT:
        return True

    per_ip = AuditLog.objects.filter(
        action=AuditAction.LOGIN_FAILED, ip=ip, created_at__gte=window_start
    ).count()
    return per_ip >= settings.LOGIN_IP_FAILURE_LIMIT
