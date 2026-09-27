"""Create an account with any role from the command line -- the bootstrap path when
DEMO_MODE=0 and no admin exists yet:

    docker compose exec web python src/manage.py create_account \\
        --email you@example.org --name "Your Name" --role admin
"""

import getpass

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User, normalize_email
from accounts.roles import Role


class Command(BaseCommand):
    help = "Create an account with the given role. Prompts for the password."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--role", required=True, choices=Role.values)
        parser.add_argument(
            "--password", help="Skip the prompt (for scripts). Visible in shell history."
        )

    def handle(self, *args, email, name, role, password=None, **options):
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
        User.objects.create_user(email, password, name=name, role=role)
        self.stdout.write(f"created {role} account {email}")
