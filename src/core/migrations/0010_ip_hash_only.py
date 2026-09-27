"""No IP address is stored: AuditLog.ip becomes ip_hash.

Existing rows are converted, not just blanked, so the login throttle's current window keeps
counting: each address becomes HMAC(derived_key("ip-hash"), address), the same function as
core.net.hash_ip (written out here so the migration does not depend on code that may change).
An address kept inside `detail` (the session revocation's `ip_of_session`) is converted too. The
address column is dropped in 0011, a separate migration: Postgres refuses to ALTER a table in the
transaction that just updated its rows while their deferred foreign-key checks are pending.
"""

import hashlib
import hmac

from django.conf import settings
from django.db import migrations, models


def _hash(ip):
    if not ip:
        return ""
    key = hmac.new(settings.SECRET_KEY.encode("utf-8"), b"ip-hash", hashlib.sha256).digest()
    return hmac.new(key, str(ip).encode("utf-8"), hashlib.sha256).hexdigest()


def convert(apps, schema_editor):
    AuditLog = apps.get_model("core", "AuditLog")
    for row in AuditLog.objects.exclude(ip__isnull=True).only("pk", "ip").iterator():
        AuditLog.objects.filter(pk=row.pk).update(ip_hash=_hash(row.ip))
    for row in AuditLog.objects.filter(detail__has_key="ip_of_session").iterator():
        detail = dict(row.detail)
        detail["session_ip_hash"] = _hash(detail.pop("ip_of_session"))[:12]
        AuditLog.objects.filter(pk=row.pk).update(detail=detail)


class Migration(migrations.Migration):
    dependencies = [("core", "0009_voting_bypass_audit_action")]

    operations = [
        migrations.AddField(model_name="auditlog", name="ip_hash",
                            field=models.CharField(blank=True, default="", max_length=64), preserve_default=False),
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
