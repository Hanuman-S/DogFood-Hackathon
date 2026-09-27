"""Import the organizers' fixture file (create-only, idempotent).

    python manage.py import_fixtures [--path acceptance/fixtures.json]

Runs on every boot when SEED_FIXTURES=1. A missing file is reported and skipped, so the portal
still boots without it.
"""

from django.core.management.base import BaseCommand

from imports.fixtures import FixtureError, import_file


class Command(BaseCommand):
    help = "Import the organizers' shared fixture data. Safe to run repeatedly."

    def add_arguments(self, parser):
        parser.add_argument("--path", help="Defaults to FIXTURES_PATH.")
        parser.add_argument("--strict", action="store_true", help="Fail if the file is missing or invalid.")

    def handle(self, *args, path=None, strict=False, **options):
        try:
            report = import_file(path)
        except FixtureError as error:
            if strict:
                raise
            self.stdout.write(f"    fixture import skipped: {error}")
            return
        for line in report.lines():
            self.stdout.write(f"    {line}")
