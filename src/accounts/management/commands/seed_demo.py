"""Seed one demo account per role, with fixed API tokens, and print the credentials.

Roles are per event, so "per role" means: a platform admin, an account that may create events
and organizes the demo events, two accounts that judge them, and one that competes in them.

    python manage.py seed_demo

Runs on every container boot when DEMO_MODE=1, so it is create-only: an account or token that
already exists is left exactly as it is (a password someone changed is not reset). It refuses
to run at all unless DEMO_MODE=1 -- the guard lives here, not in the entrypoint, so calling the
command by hand cannot bypass it.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import ApiToken, User, digest_token
from accounts.roles import Role

DEMO_EVENT_SLUG = "dogfood-live-demo"
CLOSED_EVENT_SLUG = "dogfood-archive-2026"  # closed: .dogfood.toml's submit route points here

# key in settings.DEMO_TOKENS -> (email, name, label, platform flags)
DEMO_ACCOUNTS = {
    "admin": ("admin@dogfood.local", "Demo Admin", "admin",
              {"is_platform_admin": True, "can_create_events": True}),
    "organizer": ("organizer@dogfood.local", "Demo Organizer", "organizer", {"can_create_events": True}),
    "judge_a": ("judge.a@dogfood.local", "Demo Judge A", "judge", {}),
    "judge_b": ("judge.b@dogfood.local", "Demo Judge B", "judge", {}),
    "participant": ("participant@dogfood.local", "Demo Participant", "participant", {}),
}


def _staff(event, organizer):
    """The demo organizer runs `event`; both demo judges judge it."""
    from events.models import EventMembership

    EventMembership.objects.create(event=event, user=organizer, role=Role.ORGANIZER, added_by=organizer)
    for key in ("judge_a", "judge_b"):
        judge = User.objects.get(email=DEMO_ACCOUNTS[key][0])
        EventMembership.objects.create(event=event, user=judge, role=Role.JUDGE, added_by=organizer)


def _compete(event, team, user):
    from events.models import EventMembership
    from teams.models import TeamMember

    TeamMember.objects.create(team=team, user=user)
    EventMembership.objects.create(event=event, user=user, role=Role.PARTICIPANT)


class Command(BaseCommand):
    help = "Create demo accounts (one per role) and fixed API tokens. DEMO_MODE=1 only."

    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            raise CommandError("Refusing to seed demo accounts: DEMO_MODE is not 1.")

        rows = []
        for key, (email, name, role, flags) in DEMO_ACCOUNTS.items():
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email, settings.DEMO_PASSWORD, name=name, **flags)
                state = "created"
            else:
                state = "exists"

            raw = settings.DEMO_TOKENS.get(key) or ""
            if raw and not ApiToken.objects.filter(digest=digest_token(raw)).exists():
                ApiToken.issue(user, name="demo token", raw=raw)
            rows.append((key, role, email, raw, state))

        event_state = self._seed_event()
        closed_state = self._seed_closed_event()
        self._print(rows)
        base = settings.PORTAL_BASE_URL.rstrip("/")
        self.stdout.write(f"| open demo event:   {base}/events/{DEMO_EVENT_SLUG} [{event_state}]")
        self.stdout.write(f"| closed demo event: {base}/events/{CLOSED_EVENT_SLUG} [{closed_state}]")
        self.stdout.write("+" + "-" * 78)

    def _seed_event(self):
        """An open, published event with one team and a draft project, so every page has
        something real on it. Create-only: if the slug exists, nothing is touched."""
        from datetime import timedelta

        from django.db import transaction
        from django.utils import timezone

        from events.models import CustomQuestion, Event, Prize, QuestionKind, Track
        from projects.models import Project
        from teams.models import Team

        if Event.objects.filter(slug=DEMO_EVENT_SLUG).exists():
            return "exists"
        organizer = User.objects.get(email=DEMO_ACCOUNTS["organizer"][0])
        participant = User.objects.get(email=DEMO_ACCOUNTS["participant"][0])
        now = timezone.now().replace(second=0, microsecond=0)
        with transaction.atomic():
            event = Event.objects.create(
                slug=DEMO_EVENT_SLUG,
                name="Dogfood Live Demo",
                tagline="A practice event that stays open, so you can try every step.",
                description=(
                    "## How it works\n\n"
                    "1. Form a team, or go solo.\n"
                    "2. Start your project as a **draft** and edit it as often as you like.\n"
                    "3. Submit it before the deadline. You can still edit it until then.\n\n"
                    "All times are UTC."
                ),
                starts_at=now - timedelta(days=1),
                submissions_open_at=now - timedelta(days=1),
                submissions_close_at=now + timedelta(days=30),
                judging_ends_at=now + timedelta(days=37),
                max_team_size=4,
                is_published=True,
                created_by=organizer,
            )
            _staff(event, organizer)
            tools = Track.objects.create(event=event, name="Developer tools", order=1,
                                         description="Things that make building things faster.")
            Track.objects.create(event=event, name="Security", order=2,
                                 description="Defence, detection, and making attacks expensive.")
            Track.objects.create(event=event, name="Open data", order=3,
                                 description="Public datasets turned into something useful.")
            Prize.objects.create(event=event, title="Grand prize", value="$800", rank=1)
            Prize.objects.create(event=event, title="Runner-up", value="$500", rank=2)
            Prize.objects.create(event=event, title="Best developer tool", value="$100", rank=1, track=tools)
            CustomQuestion.objects.create(
                event=event, prompt="What did you cut, and why?", kind=QuestionKind.LONG,
                required=True, order=1,
            )
            CustomQuestion.objects.create(
                event=event, prompt="Is this your first hackathon?", kind=QuestionKind.CHECKBOX, order=2,
            )
            team = Team.objects.create(event=event, name="Demo Team", captain=participant)
            _compete(event, team, participant)
            Project.objects.create(
                team=team, name="Quiet Hours", tagline="Mutes your notifications when you are in flow.",
                track=tools, last_edited_by=participant,
            )
        return "created"

    def _seed_closed_event(self):
        """An event whose submissions closed three days ago, with one submitted project.

        It gives the acceptance checker's "closed event refuses submissions" test a real closed
        event to hit, and shows every page in its read-only state. Its rows are written after the
        close, so the database trigger must be bypassed explicitly -- exactly the path organizer
        tools use, minus the audit row (there is no request at boot).
        """
        from datetime import timedelta

        from django.utils import timezone

        from core.deadlines import deadline_bypass
        from events.models import Event, Track
        from projects.models import Project, Status
        from teams.models import Team

        if Event.objects.filter(slug=CLOSED_EVENT_SLUG).exists():
            return "exists"
        organizer = User.objects.get(email=DEMO_ACCOUNTS["organizer"][0])
        participant = User.objects.get(email=DEMO_ACCOUNTS["participant"][0])
        now = timezone.now().replace(second=0, microsecond=0)
        with deadline_bypass(None, "seeding a closed demo event"):
            event = Event.objects.create(
                slug=CLOSED_EVENT_SLUG,
                name="Dogfood Archive 2026",
                tagline="A finished event: submissions are closed, so everything is read-only.",
                description="Kept so you can see what a closed event looks like.",
                starts_at=now - timedelta(days=6),
                submissions_open_at=now - timedelta(days=6),
                submissions_close_at=now - timedelta(days=3),
                judging_ends_at=now + timedelta(days=4),
                is_published=True,
                created_by=organizer,
            )
            _staff(event, organizer)
            track = Track.objects.create(event=event, name="Open category", order=1)
            team = Team.objects.create(event=event, name="Night Owls", captain=participant)
            _compete(event, team, participant)
            Project.objects.create(
                team=team, name="Sleep Debt", tagline="Tells you how many hackathons you can afford.",
                description="## What it does\n\nCounts the hours.", repo_url="https://example.org/sleep-debt",
                track=track, status=Status.SUBMITTED, submitted_at=now - timedelta(days=3, hours=2),
                last_edited_by=participant,
            )
        return "created"

    def _print(self, rows):
        out = self.stdout.write
        base = settings.PORTAL_BASE_URL.rstrip("/")
        bar = "+" + "-" * 78
        out(bar)
        out("| DEMO CREDENTIALS  (DEMO_MODE=1 -- never use these in production)")
        out(bar)
        out(f"| password for every demo account: {settings.DEMO_PASSWORD}")
        out(f"| log in at: {base}/login")
        out("|")
        for key, role, email, _raw, state in rows:
            out(f"|   {key:<12} {role:<12} {email:<28} [{state}]")
        out("|")
        out("| .dogfood.toml [auth] headers:")
        for key, _role, _email, raw, _state in rows:
            if raw and key != "admin":
                out(f'|   {key:<11} = "Authorization: Bearer {raw}"')
        out(bar)
