"""The database half of the conflict-of-interest rule (see accounts/roles.py).

Nobody may hold a competitor role (participant) and a staff role (judge, organizer) in the same
event. A CHECK constraint cannot say that, because it depends on *other rows*; a unique index
cannot either, because two staff rows (judge + organizer) are allowed. It is an exclusion
constraint: no two rows for the same (user, event) may disagree about `side`, the generated
column that maps each role to its side of the line.

`<>` on scalar columns needs the btree_gist extension, which ships with Postgres (it is a
contrib module, nothing to download).

Postgres only: on SQLite (local development without Docker) this migration does nothing and
`accounts.roles.can_compete_in` in the service layer is the only guard -- the same arrangement
as the deadline trigger.
"""

from django.db import migrations

CONSTRAINT = "membership_no_competitor_and_staff"


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    schema_editor.execute(
        f"ALTER TABLE events_eventmembership ADD CONSTRAINT {CONSTRAINT} "
        "EXCLUDE USING gist (user_id WITH =, event_id WITH =, side WITH <>)"
    )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"ALTER TABLE events_eventmembership DROP CONSTRAINT IF EXISTS {CONSTRAINT}")


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]

    operations = [migrations.RunPython(install, uninstall)]
