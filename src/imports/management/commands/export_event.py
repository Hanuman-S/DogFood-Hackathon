"""manage.py export_event <slug> <file> -- write an event's bundle (imports/bundle.py) to a file.

Run by the operator on the host, so no portal permission applies; the export is audited with no
actor. The same refusals as the button and the API (an unknown id field, a bundle too large for
the import) end the command with an error and write nothing."""

import os
import shutil

from django.core.management.base import BaseCommand, CommandError

from events.models import Event
from imports import bundle


class Command(BaseCommand):
    help = "Write an event's bundle (a zip another install can import) to a file."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("file")

    def handle(self, slug, file, **options):
        event = Event.objects.filter(slug=slug).first()
        if event is None:
            raise CommandError(f"No event with the slug {slug!r}.")
        if os.path.isdir(file):
            raise CommandError(f"{file!r} is a folder; give the bundle a file name.")
        folder = os.path.dirname(os.path.abspath(file))
        if not os.path.isdir(folder):
            raise CommandError(f"The folder {folder!r} does not exist; nothing was exported.")
        try:
            path = bundle.export_event(event, actor=None)
        except bundle.BundleError as refusal:
            raise CommandError(f"{refusal.code}: {refusal.detail}") from None
        try:
            shutil.move(path, file)
        except OSError as error:
            if os.path.exists(path):
                os.unlink(path)  # never leave the temporary bundle behind
            raise CommandError(f"Could not write {file!r}: {error.strerror or error}.") from None
        self.stdout.write(f"wrote {file}")
