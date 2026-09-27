"""Create an account from the command line -- the bootstrap path when DEMO_MODE=0 and no admin
exists yet:

    docker compose exec web python src/manage.py create_account \\
        --email you@example.org --name "Your Name" --admin

Judge, organizer and participant are roles *in an event*, granted in the portal, so the only
choices here are the two platform-wide flags.
"""

import getpass

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User, normalize_email


class Command(BaseCommand):
    help = "Create an account (optionally a platform admin). Prompts for the password."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--admin", action="store_true", help="Make a platform admin.")
        parser.add_argument(
            "--can-create-events", action="store_true", help="Allow starting new events."
        )
        parser.add_argument(
            "--password", help="Skip the prompt (for scripts). Visible in shell history."
        )

    def handle(self, *args, email, name, admin=False, can_create_events=False, password=None, **options):
        email = normalize_email(email)
        if User.objects.filter(email=email).exists():
            raise CommandError(f"An account for {email} already exists.")
        if password is None:
            password = getpass.getpass("password: ")
            if password != getpass.getpass("password (again): "):
                raise CommandError("The passwords do not match.")
        try:
            password_validation.validate_password(password, User(email=email, name=name))
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error
        User.objects.create_user(
            email, password, name=name,
            is_platform_admin=admin, can_create_events=admin or can_create_events,
        )
        kind = "platform admin" if admin else "event creator" if can_create_events else "plain"
        self.stdout.write(f"created {kind} account {email}")
