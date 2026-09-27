"""A frozen vote tally is immutable: every UPDATE of voting_votetallysnapshot is refused, like
scoring_resultsnapshot (scoring/migrations/0005). DELETE is not guarded here: the event FK is
PROTECT, so a tally cannot go with anything, and nothing in the portal deletes one.

Postgres only, like the other triggers.
"""

from django.db import migrations

FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_tally_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'dogfood_tally_immutable: vote tally % cannot be changed', OLD.id
        USING HINT = 'Freeze a new tally (compute final results) instead.';
END
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_tally_immutable ON voting_votetallysnapshot")
    schema_editor.execute(
        "CREATE TRIGGER dogfood_tally_immutable BEFORE UPDATE ON voting_votetallysnapshot "
        "FOR EACH ROW EXECUTE FUNCTION dogfood_tally_immutable()"
    )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_tally_immutable ON voting_votetallysnapshot")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_tally_immutable()")


class Migration(migrations.Migration):
    dependencies = [("voting", "0007_tally_snapshot")]

    operations = [migrations.RunPython(install, uninstall)]
