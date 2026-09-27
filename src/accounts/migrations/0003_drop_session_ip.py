"""Drop UserSession.ip after 0002 converted it (its own transaction; see core/migrations/0010)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_session_ip_hash")]

    operations = [migrations.RemoveField(model_name="usersession", name="ip")]
