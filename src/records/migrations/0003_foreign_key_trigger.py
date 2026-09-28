"""Foreign signing keys (other installs' public keys, from imported bundles) get the same guard as this
install's own (0002): no DELETE, and the only UPDATE allowed is retiring the key, once -- retired_at
from NULL to a value, every other column (kid, alg, public_key, created_at, imported_from,
imported_at) unchanged. A published key that changed or vanished would silently change which records
verify. Postgres only, like the other triggers."""

from django.db import migrations

FUNCTION = r"""
CREATE OR REPLACE FUNCTION dogfood_foreign_key_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'dogfood_signing_key_immutable: foreign signing key % cannot be deleted', OLD.kid;
    END IF;
    IF OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
       OR (NEW.kid, NEW.alg, NEW.public_key, NEW.created_at, NEW.imported_from, NEW.imported_at)
          IS DISTINCT FROM
          (OLD.kid, OLD.alg, OLD.public_key, OLD.created_at, OLD.imported_from, OLD.imported_at) THEN
        RAISE EXCEPTION 'dogfood_signing_key_immutable: foreign signing key % can only be retired, once', OLD.kid;
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
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_foreign_key_guard ON records_foreignsigningkey")
    schema_editor.execute("CREATE TRIGGER dogfood_foreign_key_guard BEFORE UPDATE OR DELETE ON "
                          "records_foreignsigningkey FOR EACH ROW EXECUTE FUNCTION dogfood_foreign_key_guard()")


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER IF EXISTS dogfood_foreign_key_guard ON records_foreignsigningkey")
    schema_editor.execute("DROP FUNCTION IF EXISTS dogfood_foreign_key_guard()")


class Migration(migrations.Migration):
    dependencies = [("records", "0002_record_and_key_triggers")]

    operations = [migrations.RunPython(install, uninstall)]
