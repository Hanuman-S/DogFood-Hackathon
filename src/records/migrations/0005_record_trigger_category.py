"""The record trigger (0002) also requires the public revocation category (0004) when a record is
revoked. revoke_category is one of the revoke fields: set once, with the others; every other column
stays compared exactly as before. Postgres only."""

from django.db import migrations

RECORD = r"""
CREATE OR REPLACE FUNCTION dogfood_record_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'dogfood_record_immutable: issued record % cannot be deleted', OLD.id
            USING HINT = 'Revoke it (with a reason) instead.';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'dogfood_record_immutable: issued record % is revoked and cannot change', OLD.id;
    END IF;
    IF NEW.revoked_at IS NULL OR NEW.revoke_reason = '' OR NEW.revoke_category = '' THEN
        RAISE EXCEPTION 'dogfood_record_immutable: issued record % can only be revoked, with a reason', OLD.id;
    END IF;
    IF (NEW.id, NEW.kind, NEW.slot, NEW.event_id, NEW.subject_user_id, NEW.payload, NEW.payload_text,
        NEW.signature, NEW.kid, NEW.is_foreign, NEW.issued_by_id, NEW.issued_at)
       IS DISTINCT FROM
       (OLD.id, OLD.kind, OLD.slot, OLD.event_id, OLD.subject_user_id, OLD.payload, OLD.payload_text,
        OLD.signature, OLD.kid, OLD.is_foreign, OLD.issued_by_id, OLD.issued_at) THEN
        RAISE EXCEPTION 'dogfood_record_immutable: issued record % can only be revoked', OLD.id;
    END IF;
    RETURN NEW;
END
$$;
"""



def install(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(RECORD)


class Migration(migrations.Migration):
    dependencies = [("records", "0004_revoke_category")]

    operations = [migrations.RunPython(install, migrations.RunPython.noop)]
