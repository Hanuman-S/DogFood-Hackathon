"""No IP address is stored: UserSession.ip becomes ip_hash (converted with the same keyed hash as
core.net.hash_ip; see core/migrations/0010), and the address column is dropped in 0003 (its own transaction; see core/migrations/0010)."""

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
    UserSession = apps.get_model("accounts", "UserSession")
    for row in UserSession.objects.exclude(ip__isnull=True).only("pk", "ip").iterator():
        UserSession.objects.filter(pk=row.pk).update(ip_hash=_hash(row.ip))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AddField(model_name="usersession", name="ip_hash",
                            field=models.CharField(blank=True, default="", max_length=64), preserve_default=False),
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
