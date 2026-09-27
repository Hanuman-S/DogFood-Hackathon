"""The database half of the weights lock (scoring.services.weights_locked / set_final_weights).

A BEFORE INSERT OR UPDATE trigger on scoring_eventscoringconfig refuses to change judge_weight or
community_weight once judging has started (events_event.judging_starts_at) or the event's vote has
opened (voting_votingconfig.opens_at), by `statement_timestamp()`. Other columns (the engine
overrides) are not affected, and inserting a row with the default 100/0 is always allowed.

`dogfood.weights_bypass = 'on'` for one transaction (scoring.services.weights_bypass, audited) is the
one way past it -- for the demo seed, which sets an event's weights after its judging has opened.

Postgres only, like the other triggers.
"""

from django.db import migrations

FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_weights_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_judging timestamptz;
    v_opens   timestamptz;
BEGIN
    IF coalesce(current_setting('dogfood.weights_bypass', true), '') = 'on' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.judge_weight = OLD.judge_weight
       AND NEW.community_weight = OLD.community_weight THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.judge_weight = 100 AND NEW.community_weight = 0 THEN
        RETURN NEW;
    END IF;
    SELECT e.judging_starts_at INTO v_judging FROM events_event e WHERE e.id = NEW.event_id;
    SELECT c.opens_at INTO v_opens FROM voting_votingconfig c WHERE c.event_id = NEW.event_id;
    IF statement_timestamp() >= v_judging OR (v_opens IS NOT NULL AND statement_timestamp() >= v_opens) THEN
        RAISE EXCEPTION 'dogfood_weights_locked: judging or voting has opened'
            USING HINT = 'The final score weights are fixed once judging or voting opens.';
    END IF;
    RETURN NEW;
END
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_weights_lock ON scoring_eventscoringconfig")
    schema_editor.execute(
        "CREATE TRIGGER dogfood_weights_lock BEFORE INSERT OR UPDATE ON scoring_eventscoringconfig "
        "FOR EACH ROW EXECUTE FUNCTION dogfood_weights_lock()"
    )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_weights_lock ON scoring_eventscoringconfig")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_weights_lock()")


class Migration(migrations.Migration):
    dependencies = [
        ("scoring", "0010_result_vote_tally_and_weights_constraint"),
        ("voting", "0001_initial"),
    ]

    operations = [migrations.RunPython(install, uninstall)]
