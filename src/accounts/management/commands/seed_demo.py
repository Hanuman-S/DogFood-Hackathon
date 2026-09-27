"""Seed one demo account per role, with fixed API tokens, and print the credentials.

Roles are per event, so "per role" means: a platform admin, an account that may create events
and organizes the demo events, two accounts that judge them, and one that competes in them.

    python manage.py seed_demo

Runs on every container boot when DEMO_MODE=1, so it is create-only: an account or token that
already exists is left exactly as it is (a password someone changed is not reset). The one
exception is a fixed demo token someone revoked on the account page: it is reactivated, because
.dogfood.toml and the acceptance checker depend on those exact tokens. It refuses
to run at all unless DEMO_MODE=1 -- the guard lives here, not in the entrypoint, so calling the
command by hand cannot bypass it.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import ApiToken, User, digest_token
from accounts.roles import Role
from scoring.services import create_standard_rubric

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


# The archive's demo teams: (email, name, team, project, tagline, repo). The first is the demo
# participant; the others are seed-only accounts that cannot log in (no password).
ARCHIVE_PROJECTS = [
    ("participant@dogfood.local", "Demo Participant", "Night Owls", "Sleep Debt",
     "Tells you how many hackathons you can afford.", "https://example.org/sleep-debt"),
    ("team.lanterns@dogfood.local", "Lantern Team", "Lanterns", "Quiet Map",
     "Finds the quietest table in the venue.", "https://example.org/quiet-map"),
    ("team.foxes@dogfood.local", "Fox Team", "Night Foxes", "Stand-up Bot",
     "Collects the team's stand-up so nobody has to stand up.", "https://example.org/standup-bot"),
    ("team.moths@dogfood.local", "Moth Team", "Moths", "Lamp Post",
     "A status page for the venue's Wi-Fi, fed by everyone's laptops.", "https://example.org/lamp-post"),
    ("team.owlets@dogfood.local", "Owlet Team", "Owlets", "Pairing Hat",
     "Suggests who to pair with, from the skills people list.", "https://example.org/pairing-hat"),
]
ARCHIVE_ASSIGNMENT_SEED = 20260926  # fixed: the same round on every boot


def _as(user):
    """A request as `user`, for calling services at boot (there is no HTTP request)."""
    from django.http import HttpRequest

    request = HttpRequest()
    request.user = user
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    request.META["HTTP_USER_AGENT"] = "seed_demo"
    return request


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
            if raw:
                existing = ApiToken.objects.filter(digest=digest_token(raw)).first()
                if existing is None:
                    ApiToken.issue(user, name="demo token", raw=raw)
                elif existing.revoked_at is not None:
                    existing.revoked_at = None
                    existing.save(update_fields=["revoked_at"])
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
                starts_at=now - timedelta(days=1, hours=1),
                submissions_open_at=now - timedelta(days=1),
                submissions_close_at=now + timedelta(days=30),
                judging_starts_at=now + timedelta(days=31),
                judging_ends_at=now + timedelta(days=37),
                max_team_size=4,
                is_published=True,
                created_by=organizer,
            )
            _staff(event, organizer)
            create_standard_rubric(event)
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
        """Dogfood Archive 2026: submissions closed three days ago and judging is under way (it
        started two days ago and ends in four), so every page shows its read-only state, the
        acceptance checker's "closed event refuses submissions" test has a real closed event to
        hit, and the judge portal has live work: five submitted projects from demo teams and one
        assignment round that gives judge.a and judge.b a queue each.

        Everything but the event row goes through the real services. Participant services only
        write while submissions are open, so the event is created with its window open, the teams
        build and submit their projects through the team and project services, and only then is
        the event's timeline moved into the past (one update; the projects' submission times move
        with it, under the audited deadline bypass the organizer tools use). The assignment round
        is `scoring.services.run_assignment` with a fixed seed, so it is the same on every boot.

        Create-only and idempotent: if the event exists nothing is created again; it only gets its
        assignment round if it has none yet (an archive seeded by an older version).
        """
        from datetime import timedelta

        from django.db import transaction
        from django.utils import timezone

        from core.deadlines import deadline_bypass
        from events.models import Event, Track
        from projects.models import Project

        event = Event.objects.filter(slug=CLOSED_EVENT_SLUG).first()
        if event is not None:
            return "exists" + self._ensure_archive_round(event)
        organizer = User.objects.get(email=DEMO_ACCOUNTS["organizer"][0])
        now = timezone.now().replace(second=0, microsecond=0)
        with transaction.atomic():
            event = Event.objects.create(
                slug=CLOSED_EVENT_SLUG,
                name="Dogfood Archive 2026",
                tagline="Submissions are closed and judging is under way: everything is read-only.",
                description="Kept so you can see what a closed event looks like, and so the demo judges "
                "have a live queue to work through.",
                # Created with the submission window open, for the participant services below;
                # moved into the past once the projects are in.
                starts_at=now - timedelta(days=6, hours=1),
                submissions_open_at=now - timedelta(days=6),
                submissions_close_at=now + timedelta(hours=1),
                judging_starts_at=now + timedelta(hours=2),
                judging_ends_at=now + timedelta(days=4),
                is_published=True,
                created_by=organizer,
            )
            _staff(event, organizer)
            create_standard_rubric(event)
            track = Track.objects.create(event=event, name="Open category", order=1)
            projects = [self._demo_project(event, track, *spec) for spec in ARCHIVE_PROJECTS]

        # The real timeline: closed three days ago, judging since two days ago, ends in four.
        closed = now - timedelta(days=3)
        with deadline_bypass(None, "seeding the archive demo event"):
            for i, project in enumerate(projects):
                Project.objects.filter(pk=project.pk).update(submitted_at=closed - timedelta(hours=2 + i))
            Event.objects.filter(pk=event.pk).update(
                submissions_close_at=closed, judging_starts_at=now - timedelta(days=2),
            )
        event.refresh_from_db()
        return "created" + self._ensure_archive_round(event)

    def _demo_project(self, event, track, email, name, team_name, project_name, tagline, repo):
        """One demo team and its submitted project, through the participant services."""
        from projects.forms import ProjectForm
        from projects.services import start_project, submit_project, update_project
        from teams.services import create_team

        user = User.objects.filter(email=email).first() or User.objects.create_user(email, None, name=name)
        request = _as(user)
        create_team(request, event, team_name)
        project = start_project(request, event, project_name)
        form = ProjectForm({
            "name": project_name, "tagline": tagline, "track": track.pk, "repo_url": repo,
            "description": f"## What it does\n\n{tagline}\n\n## Status\n\nA demo project.",
            "demo_video_url": "", "live_url": "", "tags": "demo",
        }, instance=project, event=event)
        if not form.is_valid():
            raise CommandError(f"demo project {project_name}: {form.errors.as_text()}")
        project = update_project(request, project, form)
        return submit_project(request, project)

    def _ensure_archive_round(self, event):
        """One assignment round for the archive (fixed seed), if it has none and judging is on."""
        from core.deadlines import db_now
        from core.judging import judging_closed
        from scoring.services import run_assignment

        if event.assignment_rounds.exists() or judging_closed(event, db_now()):
            return ""
        run_assignment(None, event, target=2, seed=ARCHIVE_ASSIGNMENT_SEED)
        return ", assignment round run"

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
