"""Rank an event's projects with the scoring engine, and optionally store the result.

    python src/manage.py score_event <slug>                              # print only; writes nothing
    python src/manage.py score_event <slug> --method zscore --compare raw_mean
    python src/manage.py score_event <slug> --config duplicate_policy=merge --weights functionality=2
    python src/manage.py score_event <slug> --save preview --as organizer@example.org
    python src/manage.py score_event <slug> --save final   --as organizer@example.org

--save goes through scoring.services.compute_snapshot, so every rule applies as it would in the
portal: the account named by --as must organize the event (or be a platform admin); a final is
refused before judging closes; and a final always uses the event's own configuration and weights,
so --method, --compare, --config and --weights are refused with --save final.
"""

import io

from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from events.models import Event
from scoring import services
from scoring.engine.cli import parse_weights, print_comparison, read_config
from scoring.engine.errors import EngineError
from scoring.engine import pipeline
from scoring.errors import ScoringError


class Command(BaseCommand):
    help = "Rank an event's projects with the scoring engine; --save stores a preview or final snapshot."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--method", help="primary method (default: the event's configuration)")
        parser.add_argument("--compare", help="comma-separated methods to show next to it")
        parser.add_argument("--config", help="engine config overrides: a JSON file, or key=value[,key=value]")
        parser.add_argument("--weights", help="criterion weight overrides, e.g. functionality=2,quality=1")
        parser.add_argument("--save", choices=["preview", "final"], help="store a result snapshot")
        parser.add_argument("--as", dest="actor", help="the account computing the snapshot (required with --save)")

    def handle(self, *args, slug, method, compare: str | None, config, weights, save, actor, **options):
        event = Event.objects.filter(slug=slug).first()
        if event is None:
            raise CommandError(f"No event with slug {slug!r}.")
        try:
            overrides = read_config(config) if config else {}
            if compare:
                overrides["compare"] = [m.strip() for m in compare.split(",") if m.strip()]
            weight_overrides = parse_weights(weights)
        except EngineError as error:
            raise CommandError(str(error)) from error

        if save:
            if not actor:
                raise CommandError("--save needs --as <email>: a snapshot records who computed it.")
            user = User.objects.filter(email__iexact=actor).first()
            if user is None:
                raise CommandError(f"No account with email {actor!r}.")
            try:
                snapshot = services.compute_snapshot(
                    event, save, actor=user, method=method, config=overrides or None,
                    weights=weight_overrides,
                )
            except PermissionDenied as error:
                raise CommandError(f"permission_denied (403): {error}") from error
            except ScoringError as error:
                raise CommandError(f"{error.code} ({error.status}): {error.detail}") from error
            self._print(event, snapshot.comparison_result, services.build_input(event))
            self.stdout.write(f"# saved {snapshot.kind} snapshot {snapshot.pk} (input sha256 {snapshot.input_hash})")
            for key, value in snapshot.diagnostics.items():
                self.stdout.write(f"# {key}: {value}")
            return

        try:
            cfg = services.event_config(event).with_overrides(overrides)
            inp = services.build_input(event, weights=weight_overrides)
            primary = method or cfg.primary
            result = pipeline.compare(inp, [primary, *cfg.compare], config=cfg)
        except (EngineError, ScoringError) as error:
            raise CommandError(str(error)) from error
        self._print(event, result, inp)
        self.stdout.write("# not saved (use --save preview|final --as <email> to store a snapshot)")

    def _print(self, event, comparison, inp):
        """Projects as "name (fixture id)" or "name (#pk)", judges by email, tracks by name."""
        buffer = io.StringIO()
        print_comparison(comparison, buffer, labels=services.display_labels(event, inp))
        self.stdout.write(buffer.getvalue(), ending="")
