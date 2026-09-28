"""manage.py import_event <file> --as <email> -- import an event bundle (imports/bundle_import.py) as a
new, unpublished event, acting as that account (which must be a platform admin or may create events).
The same checks and refusals as the page and the API; a refusal ends the command with an error."""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from imports import bundle_import
from imports.bundle import BundleError
from imports.bundle_validate import ImportForbidden


class Command(BaseCommand):
    help = "Import an event bundle as a new, unpublished event."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--as", dest="email", required=True, help="the importing account's email")

    def handle(self, file, email, **options):
        actor = User.objects.filter(email__iexact=email).first()
        if actor is None:
            raise CommandError(f"No account {email!r}.")
        try:
            event = bundle_import.import_event(file, actor=actor)
        except (BundleError, ImportForbidden) as refusal:
            raise CommandError(f"{refusal.code}: {refusal.detail}") from None
        self.stdout.write(f"imported as {event.slug} (unpublished)")
