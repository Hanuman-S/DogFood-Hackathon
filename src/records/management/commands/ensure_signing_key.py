"""manage.py ensure_signing_key -- run by the entrypoint after `migrate`. Makes this install's first
signing key if it has none; otherwise checks the active key's file. A missing or wrong file is reported
loudly but does not stop the boot: signing is refused until `rotate_signing_key`, and every record
already issued still verifies (records/keys.py)."""

from django.core.management.base import BaseCommand

from records import keys


class Command(BaseCommand):
    help = "Create the signing key on first boot, or check the active one."

    def handle(self, **options):
        state, kid = keys.ensure_signing_key()
        if state == "created":
            self.stdout.write(f"signing key created: {kid}")
        elif state == "ok":
            self.stdout.write(f"signing key ok: {kid}")
        else:
            self.stderr.write(self.style.ERROR(
                f"SIGNING KEY PROBLEM: the active key {kid} has no usable private key file ({state}) in "
                f"{keys.key_dir()}. Records cannot be issued until an operator runs "
                "`manage.py rotate_signing_key`. Records already issued still verify."))
