"""The database half of the results guarantees (see scoring/models.py).

* scoring_resultsnapshot: every UPDATE is refused. A snapshot is a record of what was computed;
  a new computation makes a new row. DELETE stays allowed, so a snapshot goes with its event.
* scoring_publication: an INSERT must point at a *final* snapshot of the *same* event; after
  that the row is append-only -- the only UPDATE allowed sets unpublished_at / unpublished_by
  (and its email) once, from NULL.

Postgres only, like the deadline trigger: on SQLite the migration does nothing and
`ResultSnapshot.save()` / `Publication.clean()` are the only guards.
"""

from django.db import migrations

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION dogfood_snapshot_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'dogfood_snapshot_immutable: result snapshot % cannot be changed', OLD.id
        USING HINT = 'Compute a new snapshot instead.';
END
$$;

CREATE OR REPLACE FUNCTION dogfood_publication_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_kind  text;
    v_event bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT s.kind, s.event_id INTO v_kind, v_event
          FROM scoring_resultsnapshot s WHERE s.id = NEW.snapshot_id;
        IF v_kind IS DISTINCT FROM 'final' THEN
            RAISE EXCEPTION 'dogfood_publication_not_final: only a final snapshot can be published';
        END IF;
        IF v_event IS DISTINCT FROM NEW.event_id THEN
            RAISE EXCEPTION 'dogfood_publication_wrong_event: the snapshot belongs to another event';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: append-only. Only unpublishing, once.
    IF OLD.unpublished_at IS NOT NULL
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
       OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.published_by_id IS DISTINCT FROM OLD.published_by_id
       OR NEW.published_by_email IS DISTINCT FROM OLD.published_by_email THEN
        RAISE EXCEPTION 'dogfood_publication_append_only: a publication can only be unpublished, once';
    END IF;
    RETURN NEW;
END
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # A raw cursor: the function bodies contain '%' (RAISE placeholders).
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTIONS)
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_snapshot_immutable ON scoring_resultsnapshot")
    schema_editor.execute(
        "CREATE TRIGGER dogfood_snapshot_immutable BEFORE UPDATE ON scoring_resultsnapshot "
        "FOR EACH ROW EXECUTE FUNCTION dogfood_snapshot_immutable()"
    )
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_publication_guard ON scoring_publication")
    schema_editor.execute(
        "CREATE TRIGGER dogfood_publication_guard BEFORE INSERT OR UPDATE ON scoring_publication "
        "FOR EACH ROW EXECUTE FUNCTION dogfood_publication_guard()"
    )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_snapshot_immutable ON scoring_resultsnapshot")
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_publication_guard ON scoring_publication")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_snapshot_immutable()")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_publication_guard()")


class Migration(migrations.Migration):
    dependencies = [("scoring", "0004_results_tables")]

    operations = [migrations.RunPython(install, uninstall)]
