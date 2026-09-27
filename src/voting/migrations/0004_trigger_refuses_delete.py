"""The voting trigger also refuses DELETE of a ballot or a ballot line once voting has opened.

Before voting opens there can be no ballot (inserts are refused then too), so in practice a vote,
once cast, can only leave the tally by being voided -- which keeps the row. The foreign keys into
these tables are PROTECT (events, projects, accounts, voter links), so Django never cascades into
them; this trigger also stops raw SQL. `dogfood.voting_bypass` (voting.services.voting_bypass,
audited) is the one way past it.

The vote's own row (voting_votingconfig) is guarded too, or deleting it would switch the ballot
guard off: once voting has opened, the config cannot be deleted and opens_at cannot move. (So an
event whose vote has opened cannot be deleted either, ballots or not.)

Replaces the function from 0002 (same name, CREATE OR REPLACE) and re-creates the triggers with
DELETE added. Postgres only, like 0002.
"""

from django.db import migrations

GUARDED_TABLES = ["voting_ballot", "voting_ballotline"]

FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_enforce_voting_window() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_data record;
    v_event  bigint;
    v_opens  timestamptz;
    v_closes timestamptz;
    void_cols text[] := ARRAY['voided_at', 'voided_by_id', 'void_reason'];
BEGIN
    IF TG_OP = 'DELETE' THEN row_data := OLD; ELSE row_data := NEW; END IF;

    IF coalesce(current_setting('dogfood.voting_bypass', true), '') = 'on' THEN
        RETURN row_data;
    END IF;

    IF TG_TABLE_NAME = 'voting_ballot' THEN
        IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - void_cols) = (to_jsonb(OLD) - void_cols) THEN
            RETURN NEW;  -- only the void fields changed: voiding is allowed at any time
        END IF;
        v_event := row_data.event_id;
    ELSE
        SELECT b.event_id INTO v_event FROM voting_ballot b WHERE b.id = row_data.ballot_id;
    END IF;

    SELECT c.opens_at, c.closes_at INTO v_opens, v_closes
      FROM voting_votingconfig c WHERE c.event_id = v_event;

    IF TG_OP = 'DELETE' THEN
        -- Nothing to protect before voting opens, or when the vote itself is gone.
        IF v_opens IS NOT NULL AND statement_timestamp() >= v_opens THEN
            RAISE EXCEPTION 'dogfood_voting_closed: ballots cannot be deleted once voting has opened'
                USING HINT = 'Void the ballot instead.';
        END IF;
        RETURN OLD;
    END IF;

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


CONFIG_FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_guard_voting_config() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF coalesce(current_setting('dogfood.voting_bypass', true), '') = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF statement_timestamp() >= OLD.opens_at THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'dogfood_voting_closed: a vote that has opened cannot be deleted';
        END IF;
        IF NEW.opens_at IS DISTINCT FROM OLD.opens_at OR NEW.event_id IS DISTINCT FROM OLD.event_id THEN
            RAISE EXCEPTION 'dogfood_voting_closed: a vote that has opened keeps its opening time and event';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
        cursor.execute(CONFIG_FUNCTION)
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_voting_config ON voting_votingconfig")
    schema_editor.execute(
        "CREATE TRIGGER dogfood_voting_config BEFORE UPDATE OR DELETE ON voting_votingconfig "
        "FOR EACH ROW EXECUTE FUNCTION dogfood_guard_voting_config()"
    )
    for table in GUARDED_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS dogfood_voting_window ON {table}")
        schema_editor.execute(
            f"CREATE TRIGGER dogfood_voting_window BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION dogfood_enforce_voting_window()"
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_voting_config ON voting_votingconfig")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_guard_voting_config()")
    for table in GUARDED_TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS dogfood_voting_window ON {table}")
        schema_editor.execute(
            f"CREATE TRIGGER dogfood_voting_window BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION dogfood_enforce_voting_window()"
        )


class Migration(migrations.Migration):
    dependencies = [("voting", "0003_access_modes")]

    operations = [migrations.RunPython(install, uninstall)]
