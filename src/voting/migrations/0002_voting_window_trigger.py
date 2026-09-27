"""The database half of the voting window (see voting/models.py and voting/services.py).

One PL/pgSQL function, attached BEFORE INSERT/UPDATE to voting_ballot and voting_ballotline. It
refuses the write when the event has no vote, or `statement_timestamp()` is before `opens_at` or
at/after `closes_at` -- the same half-open window the service checks first.

Voiding is the one write allowed at any time: an UPDATE of voting_ballot whose row, less the void
columns (voided_at, voided_by_id, void_reason), is unchanged. Compared as jsonb, so identity columns
added later are covered without editing this function.

DELETE is not guarded, like result snapshots: no code path deletes a ballot (voiding keeps it), and
ballots must go with their event when an event is deleted.

Code that must write outside the window sets `dogfood.voting_bypass = 'on'` for one transaction.

Postgres only: on SQLite the migration does nothing and the service check is the only guard.
"""

from django.db import migrations

GUARDED_TABLES = ["voting_ballot", "voting_ballotline"]

FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_enforce_voting_window() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_event  bigint;
    v_opens  timestamptz;
    v_closes timestamptz;
    void_cols text[] := ARRAY['voided_at', 'voided_by_id', 'void_reason'];
BEGIN
    IF coalesce(current_setting('dogfood.voting_bypass', true), '') = 'on' THEN
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'voting_ballot' THEN
        IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - void_cols) = (to_jsonb(OLD) - void_cols) THEN
            RETURN NEW;  -- only the void fields changed: voiding is allowed at any time
        END IF;
        v_event := NEW.event_id;
    ELSE
        SELECT b.event_id INTO v_event FROM voting_ballot b WHERE b.id = NEW.ballot_id;
    END IF;

    SELECT c.opens_at, c.closes_at INTO v_opens, v_closes
      FROM voting_votingconfig c WHERE c.event_id = v_event;

    IF v_opens IS NULL THEN
        RAISE EXCEPTION 'dogfood_voting_closed: this event has no vote';
    END IF;
    IF statement_timestamp() < v_opens OR statement_timestamp() >= v_closes THEN
        RAISE EXCEPTION 'dogfood_voting_closed: voting is open from % to %',
            to_char(v_opens AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            to_char(v_closes AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            USING HINT = 'Voting is not open.';
    END IF;
    RETURN NEW;
END
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # A raw cursor: the function body contains '%' (RAISE placeholders).
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
    for table in GUARDED_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS dogfood_voting_window ON {table}")
        schema_editor.execute(
            f"CREATE TRIGGER dogfood_voting_window BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION dogfood_enforce_voting_window()"
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS dogfood_voting_window ON {table}")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_enforce_voting_window()")


class Migration(migrations.Migration):
    dependencies = [("voting", "0001_initial")]

    operations = [migrations.RunPython(install, uninstall)]
