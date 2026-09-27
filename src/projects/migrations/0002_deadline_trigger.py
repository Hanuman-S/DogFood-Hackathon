"""The database half of deadline enforcement (see core/deadlines.py).

One PL/pgSQL function, attached BEFORE INSERT/UPDATE/DELETE to every table a participant can
write. It looks up the row's event and team, takes the event's close time (or the team's
extension, if later), and refuses the write when `statement_timestamp()` has reached it.

Organizer code that must write after the close sets `dogfood.deadline_bypass = 'on'` for one
transaction via `core.deadlines.deadline_bypass()`, which is audited.

Postgres only: on SQLite (local development without Docker) the migration does nothing and the
service-layer check is the only guard.
"""

from django.db import migrations

GUARDED_TABLES = [
    "projects_project",
    "projects_answer",
    "projects_projectimage",
    "projects_project_tags",
    "teams_team",
    "teams_teammember",
]

FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_enforce_deadline() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_data record;
    v_event  bigint;
    v_team   bigint;
    v_close  timestamptz;
    v_until  timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN row_data := OLD; ELSE row_data := NEW; END IF;

    IF coalesce(current_setting('dogfood.deadline_bypass', true), '') = 'on' THEN
        RETURN row_data;
    END IF;

    IF TG_TABLE_NAME = 'projects_project' THEN
        v_event := row_data.event_id;
        v_team  := row_data.team_id;
    ELSIF TG_TABLE_NAME IN ('projects_answer', 'projects_projectimage', 'projects_project_tags') THEN
        SELECT p.event_id, p.team_id INTO v_event, v_team
          FROM projects_project p WHERE p.id = row_data.project_id;
    ELSIF TG_TABLE_NAME = 'teams_teammember' THEN
        v_event := row_data.event_id;
        v_team  := row_data.team_id;
    ELSIF TG_TABLE_NAME = 'teams_team' THEN
        v_event := row_data.event_id;
        v_team  := row_data.id;
    END IF;

    IF v_event IS NULL THEN
        RETURN row_data;  -- parent row already gone in the middle of a cascade
    END IF;

    SELECT e.submissions_close_at INTO v_close FROM events_event e WHERE e.id = v_event;
    SELECT x.until INTO v_until FROM teams_teamextension x WHERE x.team_id = v_team;
    IF v_until IS NOT NULL AND v_until > v_close THEN
        v_close := v_until;
    END IF;

    IF statement_timestamp() >= v_close THEN
        RAISE EXCEPTION 'dogfood_submissions_closed: closed at %',
            to_char(v_close AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            USING HINT = 'Submissions for this event are closed.';
    END IF;
    RETURN row_data;
END
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # A raw cursor, not schema_editor.execute(): the function body contains a literal '%'
    # (PL/pgSQL's RAISE placeholder), which the driver would try to read as a parameter.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
    for table in GUARDED_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS dogfood_deadline ON {table}")
        schema_editor.execute(
            f"CREATE TRIGGER dogfood_deadline BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION dogfood_enforce_deadline()"
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS dogfood_deadline ON {table}")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_enforce_deadline()")


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0001_initial"),
        ("teams", "0001_initial"),
        ("events", "0001_initial"),
    ]

    operations = [migrations.RunPython(install, uninstall)]
