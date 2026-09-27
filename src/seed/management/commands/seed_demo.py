"""`manage.py seed_demo` -- demo accounts, fixed API tokens and an open demo event.

**This command only runs when `DEMO_MODE=1`.** The guard lives here rather than in the
entrypoint, so invoking the command directly cannot bypass it. With `DEMO_MODE=0` there are no
known passwords and no fixed tokens anywhere in the system, and the first admin is created with
`createsuperuser` as the README describes.

Why fixed token values: `.dogfood.toml` has to keep working across `docker compose down -v`. If
tokens were random per boot, the acceptance checker's config would go stale every time anyone
reset the database. The values come from environment variables with defaults in
`docker-compose.yml`, and they are hashed on the way into the database exactly like any other
token -- demo mode changes where the secret comes from, never how it is stored.

Idempotent, like the importer: re-running writes nothing. The one exception is documented on
`_refresh_window`.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from accounts.models import ApiToken, User
from core import audit, clock
from core.models import AuditAction
from events.models import (
    CustomQuestion,
    Event,
    EventMembership,
    Prize,
    QuestionKind,
    Role,
    Track,
)
from projects.models import Project, ProjectStatus
from projects.search import rebuild_search_vector
from seed.upsert import Tally, upsert
from teams.models import DEFAULT_INVITE_DAYS, Team, TeamInvite, TeamMember

# The fixture ids the brief names for the two demo judges.
JUDGE_A_EXTERNAL_ID = "jdg_02"
JUDGE_B_EXTERNAL_ID = "jdg_26"
# Captain of fixture team tm_01, and therefore the account the acceptance checker posts as.
PARTICIPANT_EMAIL = "priya1@example.org"

ADMIN_EMAIL = "admin@example.org"
ORGANIZER_EMAIL = "organizer@example.org"

DEMO_EVENT_EXTERNAL_ID = "demo_live"
DEMO_EVENT_NAME = "Dogfood Live Demo"

# A fixed salt makes the derived hash deterministic, which is what keeps this command
# idempotent: without it, `make_password` would produce a different hash every boot and rewrite
# every demo account's password row on every restart.
#
# A shared, fixed salt is normally a serious mistake. It is acceptable here, and only here,
# because this password is published in the README, is identical for every demo account, and
# exists solely so a judge can log in to a throwaway database. DEMO_MODE=0 uses none of this.
DEMO_PASSWORD_SALT = "dogfooddemosalt"


class Command(BaseCommand):
    help = "Seed demo accounts, fixed API tokens and an open demo event. Requires DEMO_MODE=1."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Skip the credentials block (still seeds).",
        )

    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            raise CommandError(
                "seed_demo refuses to run with DEMO_MODE=0.\n"
                "  It creates accounts with a published password and tokens with fixed values, "
                "which must never exist in a real deployment.\n"
                "  Bootstrap a real admin with: manage.py createsuperuser"
            )

        self.password = env("DEMO_PASSWORD", "dogfood-demo")
        self.password_hash = make_password(self.password, salt=DEMO_PASSWORD_SALT)
        self.base_url = env("PORTAL_BASE_URL", "http://localhost:8080").rstrip("/")
        self.tallies: dict[str, Tally] = {}
        self.tokens: dict[str, str] = {}

        with transaction.atomic():
            self.fixture_event = Event.objects.filter(external_id="evt_01").first()

            admin = self._seed_admin()
            organizer = self._seed_organizer()
            self._give_demo_password_to_imported_users()

            judge_a = self._find_by_external_id(JUDGE_A_EXTERNAL_ID)
            judge_b = self._find_by_external_id(JUDGE_B_EXTERNAL_ID)
            participant = self._find_participant()

            demo_event = self._seed_demo_event(organizer)
            invite = self._seed_demo_team(demo_event, participant)

            self._issue_token("organizer", organizer, "DEMO_TOKEN_ORGANIZER", "demo-organizer")
            self._issue_token("judge_a", judge_a, "DEMO_TOKEN_JUDGE_A", "demo-judge-a")
            self._issue_token("judge_b", judge_b, "DEMO_TOKEN_JUDGE_B", "demo-judge-b")
            self._issue_token("participant", participant, "DEMO_TOKEN_PARTICIPANT", "demo-participant")
            self._issue_token("admin", admin, "DEMO_TOKEN_ADMIN", "demo-admin")

        audit.record(
            AuditAction.SEED_RAN,
            event=demo_event,
            metadata={
                "counts": {name: tally.summary() for name, tally in self.tallies.items()},
            },
        )

        if not options["quiet"]:
            self._print_credentials(
                admin=admin,
                organizer=organizer,
                judge_a=judge_a,
                judge_b=judge_b,
                participant=participant,
                demo_event=demo_event,
                invite=invite,
            )

    # -- helpers ------------------------------------------------------------------------

    def tally(self, name: str) -> Tally:
        return self.tallies.setdefault(name, Tally())

    def _find_by_external_id(self, external_id: str) -> User | None:
        user = User.objects.filter(external_id=external_id).first()
        if user is None:
            self.stderr.write(
                self.style.WARNING(
                    f"  demo judge {external_id} not found: the fixture import has not run, so "
                    "no token was issued for them."
                )
            )
        return user

    def _find_participant(self) -> User | None:
        user = User.objects.filter(email=PARTICIPANT_EMAIL).first()
        if user is None:
            self.stderr.write(
                self.style.WARNING(
                    f"  demo participant {PARTICIPANT_EMAIL} not found: the fixture import has "
                    "not run, so no participant token was issued."
                )
            )
        return user

    # -- accounts -----------------------------------------------------------------------

    def _seed_admin(self) -> User:
        admin, outcome = upsert(
            User,
            lookup={"email": ADMIN_EMAIL},
            fields={
                "display_name": "Demo Platform Admin",
                "is_platform_admin": True,
                "is_staff": True,
                "is_superuser": True,
                "can_create_events": True,
                "password": self.password_hash,
            },
            validate_exclude=("password", "last_login"),
        )
        self.tally("admin").record(outcome)
        return admin

    def _seed_organizer(self) -> User:
        organizer, outcome = upsert(
            User,
            lookup={"email": ORGANIZER_EMAIL},
            fields={
                "display_name": "Demo Organizer",
                "can_create_events": True,
                "password": self.password_hash,
            },
            validate_exclude=("password", "last_login"),
        )
        self.tally("organizer").record(outcome)

        # Organizer of the imported fixture event, so there is someone who can manage it.
        if self.fixture_event:
            _, outcome = upsert(
                EventMembership,
                lookup={
                    "user": organizer,
                    "event": self.fixture_event,
                    "role": Role.ORGANIZER,
                },
                fields={},
            )
            self.tally("organizer memberships").record(outcome)

        return organizer

    def _give_demo_password_to_imported_users(self) -> None:
        """Give every imported fixture account the published demo password.

        Imported accounts arrive with an unusable password. In demo mode they need a usable one
        so a judge can actually log in as a fixture judge or participant and see what those
        roles see.

        One `make_password` call, one shared deterministic hash, and a single bulk UPDATE
        restricted to the rows that do not already have it -- so this costs one key derivation
        rather than 121, and nothing at all on a re-run.
        """
        # Precisely the accounts this seeding owns: the two named demo logins, judges (which carry
        # an `external_id`), and team members (identified by their membership of the fixture
        # event). Matching on the email domain instead would also rewrite the password of a real
        # person who happened to sign up with an example.org address, which is not this command's
        # business.
        #
        # This is a targeted write rather than part of the `upsert` field list, and deliberately
        # so: `upsert` is create-only, so an existing account's password would otherwise never be
        # reset -- and the boot banner would then print a password that does not work. The demo
        # credential being *true* is the whole point of printing it.
        imported = Q(external_id__isnull=False) | Q(email__in=[ADMIN_EMAIL, ORGANIZER_EMAIL])
        if self.fixture_event is not None:
            imported |= Q(event_memberships__event=self.fixture_event)

        stale = (
            User.objects.filter(imported)
            .exclude(password=self.password_hash)
            .distinct()
        )
        # `update()` cannot follow the join the Q above may introduce, so the ids are resolved
        # first and the write is a single UPDATE ... WHERE id IN (...).
        stale_ids = list(stale.values_list("pk", flat=True))
        updated = (
            User.objects.filter(pk__in=stale_ids).update(password=self.password_hash)
            if stale_ids
            else 0
        )

        total_imported = User.objects.filter(imported).distinct().count()
        tally = self.tally("accounts given the demo password")
        tally.created = updated
        tally.unchanged = total_imported - updated

    def _issue_token(self, label: str, user: User | None, env_var: str, default_suffix: str) -> None:
        """Create (or adopt) the fixed demo token for one role."""
        if user is None:
            return

        plaintext = env(env_var, f"dogfood-{default_suffix}-token-do-not-use-in-production")
        digest = ApiToken.hash_token(plaintext)

        existing = ApiToken.objects.filter(token_hash=digest).first()
        if existing is None:
            ApiToken.objects.create(user=user, name=f"demo {label}", token_hash=digest)
            self.tally("demo tokens").record("created")
        else:
            # A fixed plaintext means a fixed hash, so the row cannot simply be recreated if
            # somebody revoked it while testing revocation. Restoring it is the right behaviour
            # in demo mode specifically, because this token is printed at every boot and
            # documented in .dogfood.toml -- leaving it revoked would silently break the
            # acceptance checker.
            changed = []
            if existing.revoked_at is not None:
                existing.revoked_at = None
                changed.append("revoked_at")
            if existing.user_id != user.pk:
                existing.user = user
                changed.append("user")
            if changed:
                existing.save(update_fields=changed)
                self.tally("demo tokens").record("updated")
            else:
                self.tally("demo tokens").record("unchanged")

        self.tokens[label] = plaintext

    # -- the open demo event ------------------------------------------------------------

    def _seed_demo_event(self, organizer: User) -> Event:
        """An event whose submission window is open, so the demo can show the real flow.

        The imported fixture event closed in March 2026 and must stay closed -- that is what
        makes the acceptance checker's third T1 check meaningful. But a portal where every
        event is closed cannot demonstrate drafting, editing and submitting. Hence a second
        event, open by construction.
        """
        now = clock.now()
        opened_at = now - dt.timedelta(days=1)
        closes_at = now + dt.timedelta(days=7)

        existing = Event.objects.filter(external_id=DEMO_EVENT_EXTERNAL_ID).first()
        window = self._refresh_window(existing, opened_at, closes_at)
        needs_refresh = existing is not None and window[1] != existing.submissions_close_at

        event, outcome = upsert(
            Event,
            lookup={"external_id": DEMO_EVENT_EXTERNAL_ID},
            fields={
                "name": DEMO_EVENT_NAME,
                "slug": "dogfood-live-demo",
                "description": (
                    "Demo event created by `manage.py seed_demo` (DEMO_MODE=1). Its submission "
                    "window is open, so you can create a draft, edit it and submit it. The "
                    "imported fixture event is closed and must stay closed."
                ),
                "starts_at": window[0],
                "submissions_open_at": window[0],
                "submissions_close_at": window[1],
                "judging_ends_at": window[1] + dt.timedelta(days=7),
                "max_team_size": 4,
                "gallery_public": True,
                "created_by": organizer,
            },
        )
        self.tally("demo event").record(outcome)

        if needs_refresh:
            # `upsert` is create-only, so it will not move an existing event's window -- which is
            # exactly right for the fixture event and exactly wrong here. The refresh is
            # therefore an explicit, narrow write of just the four window columns, so that the
            # one place allowed to move a deadline is visible rather than a side effect of a
            # generic helper.
            Event.objects.filter(pk=event.pk).update(
                starts_at=window[0],
                submissions_open_at=window[0],
                submissions_close_at=window[1],
                judging_ends_at=window[1] + dt.timedelta(days=7),
            )
            event.refresh_from_db()
            self.tally("demo event window refreshed").record("updated")

        _, outcome = upsert(
            EventMembership,
            lookup={"user": organizer, "event": event, "role": Role.ORGANIZER},
            fields={},
        )
        self.tally("demo event organizer").record(outcome)

        self._seed_demo_tracks(event)
        self._seed_demo_prizes(event)
        self._seed_demo_questions(event)
        return event

    def _refresh_window(
        self, existing: Event | None, opened_at, closes_at
    ) -> tuple[dt.datetime, dt.datetime]:
        """Decide the demo event's window on a re-run.

        The only intentional non-idempotency in this command. If the demo event's window has
        already closed -- the stack was left running for over a week -- it is shifted forward so
        the demo still demonstrates something. An open window is left exactly as it was, so
        restarting the container does not move a deadline a demo user is working against.
        """
        if existing is None:
            return opened_at, closes_at
        if clock.now() >= existing.submissions_close_at:
            self.stdout.write(
                "  demo event window had expired; shifting it forward so the demo stays usable."
            )
            return opened_at, closes_at
        return existing.submissions_open_at, existing.submissions_close_at

    def _seed_demo_tracks(self, event: Event) -> None:
        """Mirror the fixture event's eight tracks, so both events are comparable.

        Read from the database rather than hardcoded: the track names are the organizers' data,
        and copying them into source would be a second, drifting copy of their file.
        """
        tally = self.tally("demo tracks")
        source_names = []
        if self.fixture_event:
            source_names = list(
                Track.objects.filter(event=self.fixture_event)
                .order_by("order", "name")
                .values_list("name", flat=True)
            )
        if not source_names:
            # No fixture import ran, so there is nothing to mirror. One generic track keeps the
            # demo event usable instead of leaving it un-submittable.
            source_names = ["General"]
            tally.skip("fixture tracks unavailable; created a single 'General' track instead")

        for order, name in enumerate(source_names):
            _, outcome = upsert(
                Track,
                lookup={"external_id": f"{DEMO_EVENT_EXTERNAL_ID}:trk:{order:02d}"},
                fields={"event": event, "name": name, "order": order},
            )
            tally.record(outcome)

    def _seed_demo_prizes(self, event: Event) -> None:
        tally = self.tally("demo prizes")
        prizes = [
            ("Grand Prize", "The overall winner.", "800 USD"),
            ("Runner Up", "Second place.", "500 USD"),
            ("Best Judging Engine", "Most defensible scoring and isolation.", "100 USD"),
        ]
        for order, (name, description, value) in enumerate(prizes):
            _, outcome = upsert(
                Prize,
                lookup={"external_id": f"{DEMO_EVENT_EXTERNAL_ID}:prize:{order}"},
                fields={
                    "event": event,
                    "name": name,
                    "description": f"{description} (demo data)",
                    "value_text": value,
                    "order": order,
                },
            )
            tally.record(outcome)

    def _seed_demo_questions(self, event: Event) -> None:
        """One of each interesting question kind, including a required one.

        A required question is what makes the submit-time validation visible in a demo: a draft
        saves without it, and submitting refuses until it is answered.
        """
        tally = self.tally("demo custom questions")
        questions = [
            {
                "prompt": "How did your team build this? Describe the architecture.",
                "kind": QuestionKind.LONG_TEXT,
                "required": True,
                "show_in_gallery": True,
                "choices": [],
            },
            {
                "prompt": "Did your team use AI coding tools?",
                "kind": QuestionKind.BOOLEAN,
                "required": False,
                "show_in_gallery": True,
                "choices": [],
            },
            {
                "prompt": "How did you hear about this event?",
                "kind": QuestionKind.CHOICE,
                "required": False,
                "show_in_gallery": False,
                "choices": ["A friend", "Social media", "University", "Other"],
            },
        ]
        for order, spec in enumerate(questions):
            _, outcome = upsert(
                CustomQuestion,
                lookup={"external_id": f"{DEMO_EVENT_EXTERNAL_ID}:q:{order}"},
                fields={"event": event, "order": order, **spec},
            )
            tally.record(outcome)

    def _seed_demo_team(self, event: Event, participant: User | None) -> TeamInvite | None:
        """A team with a draft project in the open event, plus a copyable invite link.

        The captain is the same fixture participant the acceptance checker uses. That is not a
        shortcut: it demonstrates the point of per-event roles, since the same person is a
        participant in two different events with two different teams.
        """
        if participant is None:
            return None

        team, outcome = upsert(
            Team,
            lookup={"external_id": f"{DEMO_EVENT_EXTERNAL_ID}:team"},
            fields={"event": event, "name": "Kiln Works", "created_by": participant},
        )
        self.tally("demo team").record(outcome)

        _, outcome = upsert(
            EventMembership,
            lookup={"user": participant, "event": event, "role": Role.PARTICIPANT},
            fields={},
        )
        self.tally("demo team membership").record(outcome)

        _, outcome = upsert(
            TeamMember,
            lookup={"event": event, "user": participant},
            fields={"team": team, "is_captain": True},
        )
        self.tally("demo team member").record(outcome)

        project, outcome = upsert(
            Project,
            lookup={"external_id": f"{DEMO_EVENT_EXTERNAL_ID}:project"},
            fields={
                "event": event,
                "team": team,
                "name": "Quiet Hours",
                "tagline": "A draft project, so the demo can show editing and submitting.",
                "description": (
                    "This is **demo content** created by `seed_demo`.\n\n"
                    "It is a draft: it does not appear in the public gallery, and it will not "
                    "appear there unless someone submits it before the window closes."
                ),
                "status": ProjectStatus.DRAFT,
                "submitted_at": None,
            },
            validate_exclude=("search_vector",),
        )
        self.tally("demo draft project").record(outcome)
        rebuild_search_vector(project)

        # Fixed token so the printed invite URL survives `docker compose down -v` and can be
        # written into documentation.
        plaintext = env("DEMO_INVITE_TOKEN", "dogfood-demo-invite-token-not-for-production")
        digest = TeamInvite.hash_token(plaintext)
        invite = TeamInvite.objects.filter(token_hash=digest).first()
        if invite is None:
            invite = TeamInvite.objects.create(
                team=team,
                token_hash=digest,
                created_by=participant,
                expires_at=clock.now() + dt.timedelta(days=DEFAULT_INVITE_DAYS),
            )
            self.tally("demo invite").record("created")
        else:
            changed = []
            if invite.revoked_at is not None:
                invite.revoked_at = None
                changed.append("revoked_at")
            if invite.expires_at <= clock.now():
                invite.expires_at = clock.now() + dt.timedelta(days=DEFAULT_INVITE_DAYS)
                changed.append("expires_at")
            if changed:
                invite.save(update_fields=changed)
                self.tally("demo invite").record("updated")
            else:
                self.tally("demo invite").record("unchanged")

        self.invite_plaintext = plaintext
        return invite

    # -- output -------------------------------------------------------------------------

    def _print_credentials(self, *, admin, organizer, judge_a, judge_b, participant, demo_event, invite):
        def header(name: str) -> str:
            return f'"Authorization: Bearer {self.tokens.get(name, "<not issued>")}"'

        emails = [
            u.email
            for u in (admin, organizer, judge_a, judge_b, participant)
            if u is not None
        ]

        lines = [
            "",
            "  ===== DOGFOOD DEMO CREDENTIALS (DEMO_MODE=1 - disable in production) =====",
            f"  organizer   = {header('organizer')}",
            f"  judge_a     = {header('judge_a')}",
            f"  judge_b     = {header('judge_b')}",
            f"  participant = {header('participant')}",
            f"  admin       = {header('admin')}",
            "",
            f"  Web logins: {', '.join(emails)} / {self.password}",
            f"  Every imported fixture account also uses the password: {self.password}",
        ]
        if invite is not None:
            lines.append(
                f"  Live demo invite: {self.base_url}/invite/{self.invite_plaintext}"
            )
        lines += [
            f"  Open demo event:  {self.base_url}/events/{demo_event.slug}",
            "",
            "  The four Bearer headers above are exactly the strings .dogfood.toml expects.",
            "  ==========================================================================",
            "",
        ]
        self.stdout.write("\n".join(lines))


def env(name: str, default: str) -> str:
    """Read a demo setting, treating a blank value as absent (see config.settings.env)."""
    import os

    value = os.environ.get(name)
    return default if value is None or value == "" else value
