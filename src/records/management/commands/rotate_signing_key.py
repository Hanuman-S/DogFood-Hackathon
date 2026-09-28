"""manage.py rotate_signing_key -- make a new active signing key. The old one is retired, stays
published at /.well-known/dogfood-signing-keys.json, and its file stays on disk; records it signed
keep verifying. Audited."""

from django.core.management.base import BaseCommand

from records import keys


class Command(BaseCommand):
    help = "Make a new active signing key; retire the old one (still published)."

    def handle(self, **options):
        old, new = keys.rotate()
        self.stdout.write(f"signing key rotated: {old or '(none)'} -> {new}")
