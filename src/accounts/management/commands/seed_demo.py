"""Seed one demo account per role, with fixed API tokens, and print the credentials.

Roles are per event, so "per role" means: a platform admin, an account that may create events
and organizes the demo events, two accounts that judge them, and one that competes in them.

    python manage.py seed_demo            # accounts, the live and archive events, the archive's vote
    python manage.py seed_demo --votes    # after import_fixtures: a closed vote on the fixture event

Runs on every container boot when DEMO_MODE=1, so it is create-only: an account or token that
already exists is left exactly as it is (a password someone changed is not reset). The one
exception is a fixed demo token someone revoked on the account page: it is reactivated, because
.dogfood.toml and the acceptance checker depend on those exact tokens. It refuses
to run at all unless DEMO_MODE=1 -- the guard lives here, not in the entrypoint, so calling the
command by hand cannot bypass it.
"""

import secrets

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

# The archive's community vote (T3): seed-only voter accounts (no password: they cannot log in).
# (email, name, documentation-range IP the ballot "came from", {project name: credits}).
ARCHIVE_VOTERS = [
    ("voter.ada@dogfood.local", "Ada V.", "192.0.2.11", {"Quiet Map": 9, "Pairing Hat": 4, "Lamp Post": 1}),
    ("voter.ben@dogfood.local", "Ben V.", "192.0.2.12", {"Stand-up Bot": 16}),
    ("voter.cai@dogfood.local", "Cai V.", "192.0.2.13", {"Sleep Debt": 4, "Quiet Map": 4, "Lamp Post": 4, "Pairing Hat": 4}),
    ("voter.dee@dogfood.local", "Dee V.", "192.0.2.14", {"Pairing Hat": 9, "Sleep Debt": 1}),
    ("voter.eli@dogfood.local", "Eli V.", "192.0.2.15", {"Quiet Map": 16}),
    ("voter.fay@dogfood.local", "Fay V.", "192.0.2.16", {"Lamp Post": 9, "Stand-up Bot": 4, "Quiet Map": 1}),
]
# One deliberately suspicious cluster: four accounts, one network, identical spread ballots within
# minutes. The integrity page flags it twice (network burst, identical ballots); nothing is voided.
ARCHIVE_CLUSTER_IP = "198.51.100.66"
ARCHIVE_CLUSTER = [(f"voter.cluster{i}@dogfood.local", f"Cluster Voter {i}") for i in range(1, 5)]
ARCHIVE_CLUSTER_BALLOT = {"Stand-up Bot": 9, "Lamp Post": 4, "Sleep Debt": 1}
ARCHIVE_COMMUNITY_WEIGHT = 20


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

    def add_arguments(self, parser):
        parser.add_argument("--votes", action="store_true",
                            help="after import_fixtures: give the fixture event a closed community vote")

    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            raise CommandError("Refusing to seed demo accounts: DEMO_MODE is not 1.")
        if options["votes"]:
            self.stdout.write(f"--> fixture event vote: {self._seed_fixture_vote()}")
            return

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
        closed_state += self._seed_archive_vote()
        self._drop_old_demo_tag()
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

    def _seed_archive_vote(self):
        """The archive's community vote, open for its whole judging phase: quadratic, 16 credits,
        logged-in voting, community weight 20 (set through the audited weights bypass: judging has
        already opened, so the weights are otherwise locked). Ten seeded ballots through the real
        voting service, one cluster of them deliberately suspicious. Create-only: nothing happens if
        the archive already has a vote.

        "Only accounts created before voting opened" is OFF here, on purpose: the demo accounts are
        created at boot, after this vote "opened", and the demo participant must be able to vote.
        """
        from datetime import timedelta

        from core.audit import Origin
        from core.net import hash_ip
        from events.models import Event
        from projects.models import Project
        from scoring.models import EventScoringConfig
        from scoring.services import weights_bypass
        from voting import services as voting
        from voting.models import AccessMode, Method, VotingConfig

        event = Event.objects.filter(slug=CLOSED_EVENT_SLUG).first()
        if event is None or VotingConfig.objects.filter(event=event).exists():
            return ""
        organizer = User.objects.get(email=DEMO_ACCOUNTS["organizer"][0])
        VotingConfig.objects.create(
            event=event, opens_at=event.submissions_close_at, closes_at=event.judging_ends_at,
            access_mode=AccessMode.AUTHENTICATED, method=Method.QUADRATIC, credit_budget=16,
            accounts_before_open_only=False, ballot_secret=secrets.token_hex(32), created_by=organizer,
        )
        with weights_bypass("demo seed: the archive's final score is 80% judges, 20% community",
                            actor=organizer, subject=event.slug):
            EventScoringConfig.objects.update_or_create(event=event, defaults={
                "judge_weight": 100 - ARCHIVE_COMMUNITY_WEIGHT, "community_weight": ARCHIVE_COMMUNITY_WEIGHT,
                "updated_by": organizer})
        names = {p.name: p.pk for p in Project.objects.filter(event=event)}
        joined = event.submissions_close_at - timedelta(days=7)

        def voter(email, name):
            user = User.objects.filter(email=email).first() or User.objects.create_user(email, None, name=name)
            return user

        def cast(user, ip, ballot):
            ip_hash = hash_ip(ip)
            voting.cast(event, voting.Voter(user), ip_hash, {names[n]: c for n, c in ballot.items()}, user,
                        origin=Origin(ip_hash=ip_hash, user_agent="seed_demo"))

        for email, name, ip, ballot in ARCHIVE_VOTERS:
            user = voter(email, name)
            User.objects.filter(pk=user.pk, date_joined__gt=joined).update(date_joined=joined)
            user.refresh_from_db()
            cast(user, ip, ballot)
        for email, name in ARCHIVE_CLUSTER:
            cast(voter(email, name), ARCHIVE_CLUSTER_IP, ARCHIVE_CLUSTER_BALLOT)
        return ", community vote seeded"

    def _seed_fixture_vote(self):
        """Give the organizers' fixture event (Sample Hack 2026) a community vote that has already
        closed (its window is the event's judging window, in March 2026), with no ballots. It lets
        t3-check show a late vote refused as 409 voting_closed on real data, and the event's final
        result still computes and publishes: a final after the close freezes the (empty) tally.
        Create-only."""
        from events.models import Event
        from imports.models import FixtureRef
        from voting import services as voting
        from voting.models import AccessMode, Method, VotingConfig

        ref = FixtureRef.objects.filter(kind=FixtureRef.Kind.EVENT, external_id="evt_01").first()
        event = Event.objects.filter(pk=ref.object_id).first() if ref else None
        if event is None:
            return "no fixture event"
        if VotingConfig.objects.filter(event=event).exists():
            return "exists"
        VotingConfig.objects.create(
            event=event, opens_at=event.submissions_close_at, closes_at=event.judging_ends_at,
            access_mode=AccessMode.AUTHENTICATED, method=Method.QUADRATIC, credit_budget=16,
            ballot_secret=secrets.token_hex(32),
        )
        return f"created (closed at {event.judging_ends_at.isoformat()})"

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
            "demo_video_url": "", "live_url": "", "tags": "",
        }, instance=project, event=event)
        if not form.is_valid():
            raise CommandError(f"demo project {project_name}: {form.errors.as_text()}")
        project = update_project(request, project, form)
        return submit_project(request, project)

    def _drop_old_demo_tag(self):
        """Earlier seeds tagged every demo project "demo", which showed in the gallery as a filter
        chip ("demo 5") meaning nothing. Take it off the two demo events' projects (only those, and
        only that tag), and drop the tag once nothing uses it. Idempotent, so it runs every boot.
        The archive is closed, so the tags table is behind the deadline trigger: the seed's usual
        bypass, as when the archive is created."""
        from core.deadlines import deadline_bypass
        from projects.models import Project, Tag

        tag = Tag.objects.filter(name="demo").first()
        if tag is None:
            return
        tagged = Project.objects.filter(event__slug__in=[DEMO_EVENT_SLUG, CLOSED_EVENT_SLUG], tags=tag)
        if tagged.exists():
            with deadline_bypass(None, "demo seed: removing the old 'demo' tag from demo projects"):
                for project in tagged:
                    project.tags.remove(tag)
        if not tag.projects.exists():
            tag.delete()

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
