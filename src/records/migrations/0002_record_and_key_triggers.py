"""Issued records and signing keys are immutable where it matters. Postgres only, like the other
triggers.

records_issuedrecord (BEFORE UPDATE OR DELETE):
* DELETE is refused: a record someone may already have shown to others is revoked, never removed.
* An UPDATE may only revoke: revoked_at, revoked_by_id and revoke_reason go from unset to set, once,
  with a reason and every other column unchanged. Un-revoking, re-revoking, and any other change are
  refused.

records_signingkey (BEFORE UPDATE OR DELETE):
* DELETE is refused: a retired key must stay published, or the records it signed stop verifying.
* An UPDATE may only retire the key (retired_at from unset to set, once); nothing else changes.
"""

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
    IF NEW.revoked_at IS NULL OR NEW.revoke_reason = '' THEN
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

KEY = r"""
CREATE OR REPLACE FUNCTION dogfood_signing_key_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'dogfood_signing_key_immutable: signing key % cannot be deleted', OLD.kid
            USING HINT = 'Retired keys stay published so the records they signed keep verifying.';
    END IF;
    IF OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
       OR (NEW.kid, NEW.alg, NEW.public_key, NEW.created_at) IS DISTINCT FROM
          (OLD.kid, OLD.alg, OLD.public_key, OLD.created_at) THEN
        RAISE EXCEPTION 'dogfood_signing_key_immutable: signing key % can only be retired, once', OLD.kid;
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
        cursor.execute(KEY)
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_record_guard ON records_issuedrecord")
    schema_editor.execute("CREATE TRIGGER dogfood_record_guard BEFORE UPDATE OR DELETE ON records_issuedrecord "
                          "FOR EACH ROW EXECUTE FUNCTION dogfood_record_guard()")
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_signing_key_guard ON records_signingkey")
    schema_editor.execute("CREATE TRIGGER dogfood_signing_key_guard BEFORE UPDATE OR DELETE ON records_signingkey "
                          "FOR EACH ROW EXECUTE FUNCTION dogfood_signing_key_guard()")


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_record_guard ON records_issuedrecord")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_record_guard()")
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_signing_key_guard ON records_signingkey")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_signing_key_guard()")


class Migration(migrations.Migration):
    dependencies = [("records", "0001_initial")]

    operations = [migrations.RunPython(install, uninstall)]
