"""List the registered scoring methods: name, version and capabilities.

    python src/manage.py score_methods
"""

import io

from django.core.management.base import BaseCommand

from scoring.engine.cli import print_methods


class Command(BaseCommand):
    help = "List the registered scoring methods with their version and capabilities."

    def handle(self, *args, **options):
        buffer = io.StringIO()
        print_methods(buffer)
        self.stdout.write(buffer.getvalue(), ending="")
