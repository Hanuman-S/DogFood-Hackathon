"""Drop AuditLog.ip after 0010 converted it (its own transaction; see 0010)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0010_ip_hash_only")]

    operations = [
        migrations.RemoveIndex(model_name="auditlog", name="audit_ip_idx"),
        migrations.RemoveField(model_name="auditlog", name="ip"),
        migrations.AddIndex(model_name="auditlog",
                            index=models.Index(fields=["action", "ip_hash", "created_at"], name="audit_ip_hash_idx")),
    ]
