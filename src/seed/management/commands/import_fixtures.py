"""`manage.py import_fixtures [path]`

Loads the organizers' shared fixture file. Idempotent: running it twice reports every row as
unchanged and writes nothing. The entrypoint runs it on every boot for exactly that reason.

The real work is in `seed.importer`; this command is the CLI wrapper, so the import logic can be
tested without a subprocess.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core import audit
from core.models import AuditAction
from seed.importer import FixtureImporter


class Command(BaseCommand):
    help = "Import the organizer fixture file (acceptance/fixtures.json). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help=f"Path to fixtures.json. Defaults to FIXTURES_PATH ({settings.FIXTURES_PATH}).",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress the import report; still writes the audit entry.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help=(
                "Overwrite existing rows that differ from the fixture. OFF by default, because "
                "this command runs on every boot and must not revert deliberate edits such as an "
                "organizer extending a deadline. Run it by hand when you really do want the "
                "fixture to win."
            ),
        )

    def handle(self, *args, **options):
        path = Path(options["path"] or settings.FIXTURES_PATH)

        if not path.exists():
            # Deliberately not an error. compose bind-mounts the fixture file, but someone
            # running the bare image without that mount should get a portal that boots empty
            # and says why -- not a container that crash-loops on a missing input file.
            self.stderr.write(
                self.style.WARNING(
                    f"fixtures file not found at {path}; skipping import.\n"
                    "  The portal will start with no events. Mount the organizers' "
                    "fixtures.json there, or pass a path:\n"
                    "    manage.py import_fixtures /path/to/fixtures.json"
                )
            )
            return

        importer = FixtureImporter(path, sync=options["sync"])
        report = importer.run()

        if not options["quiet"]:
            self.stdout.write(report.render())

        # Surfaced on stderr as well, because a divergence is the one outcome an operator
        # actually needs to notice in a wall of boot output.
        if report.preserved_total and not options["sync"]:
            self.stderr.write(
                self.style.WARNING(
                    f"  {report.preserved_total} existing row(s) differ from the fixture and were "
                    "left unchanged. See the PRESERVED section above."
                )
            )

        audit.record(
            AuditAction.IMPORT_RAN,
            event=importer.event,
            metadata={
                "source": str(path),
                "sync": options["sync"],
                "counts": {
                    name: {
                        "created": tally.created,
                        "updated": tally.updated,
                        "unchanged": tally.unchanged,
                        "preserved": tally.preserved,
                        "skipped": len(tally.skipped),
                    }
                    for name, tally in report.tallies.items()
                },
                "duplicates_flagged": len(report.duplicates),
                "preserved": [
                    f"{name}: {entry}"
                    for name, tally in report.tallies.items()
                    for entry in tally.divergences
                ],
            },
        )
