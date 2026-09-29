import pytest
from django.test import Client

from accounts.models import User
from accounts.roles import ADMIN, Role

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _media_in_tmp(settings, tmp_path):
    """Uploaded files (and the demo seed's pictures) go to this test's own temporary folder, never
    to the repo's media/ (the suite runs against the mounted working tree). A test that sets
    MEDIA_ROOT itself still wins: its override is applied inside this one."""
    settings.MEDIA_ROOT = str(tmp_path / "media")


def _staff_pool_event():
    """An unpublished event that exists only to give test judges a judge role somewhere.

    Roles are per event, so "a judge account" means "an account that judges some event".
    """
    from datetime import timedelta

    from django.utils import timezone

    from events.models import Event

    now = timezone.now()
    event, _ = Event.objects.get_or_create(
        slug="test-judging-pool",
        defaults=dict(
            name="Judging pool", starts_at=now - timedelta(days=1, hours=1),
            submissions_open_at=now - timedelta(days=1), submissions_close_at=now + timedelta(days=2),
            judging_starts_at=now + timedelta(days=2, hours=1), judging_ends_at=now + timedelta(days=5),
        ),
    )
    return event


@pytest.fixture
def make_user(db):
    """An account shaped like the given role.

    participant -> a plain account (it becomes a participant by joining a team)
    judge       -> a judge of a throwaway "judging pool" event
    organizer   -> may create events (the organizer of an event is added by make_event)
    admin       -> a platform admin
    """

    def make(role=Role.PARTICIPANT, email=None, password=PASSWORD, name="Test User"):
        from events.models import EventMembership

        email = email or f"{role}-{User.objects.count() + 1}@example.org"
        user = User.objects.create_user(
            email, password, name=name,
            is_platform_admin=role == ADMIN,
            can_create_events=role in (ADMIN, Role.ORGANIZER),
        )
        if role == Role.JUDGE:
            EventMembership.objects.create(user=user, event=_staff_pool_event(), role=Role.JUDGE)
        return user

    return make


@pytest.fixture
def login_client(make_user):
    """A Client logged in through the real login form, so signals and tracking all run."""

    def make(role=Role.PARTICIPANT, user=None, ip="10.0.0.1", **client_kwargs):
        user = user or make_user(role=role)
        client = Client(REMOTE_ADDR=ip, HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux) Firefox/130.0",
                        **client_kwargs)
        response = client.post("/login", {"email": user.email, "password": PASSWORD})
        assert response.status_code == 302, response.content[:500]
        client.user = user
        return client

    return make


# --- events, teams, projects -----------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402


@pytest.fixture
def make_event(make_user):
    """A published event whose submission window is open, with one organizer."""
    from events.models import Event, EventMembership

    def make(slug=None, organizer=None, published=True, max_team_size=4, **dates):
        """Dates default to an event whose submissions are open; any may be overridden. Defaults
        for the later dates follow the earlier ones, so the timeline stays strictly ordered."""
        now = timezone.now()
        organizer = organizer or make_user(role=Role.ORGANIZER)
        opens = dates.get("submissions_open_at", now - timedelta(days=1))
        closes = dates.get("submissions_close_at", now + timedelta(days=2))
        judging_starts = dates.get("judging_starts_at", closes + timedelta(hours=1))
        judging_ends = dates.get("judging_ends_at", max(now + timedelta(days=5), judging_starts + timedelta(days=1)))
        event = Event.objects.create(
            slug=slug or f"event-{Event.objects.count() + 1}",
            name="Test Hack",
            starts_at=dates.get("starts_at", opens - timedelta(hours=1)),
            submissions_open_at=opens,
            submissions_close_at=closes,
            judging_starts_at=judging_starts,
            judging_ends_at=judging_ends,
            results_at=dates.get("results_at"),
            max_team_size=max_team_size,
            is_published=published,
            created_by=organizer,
        )
        EventMembership.objects.create(event=event, user=organizer, role=Role.ORGANIZER, added_by=organizer)
        event.organizer = organizer
        return event

    return make


@pytest.fixture
def make_team(make_user):
    from events.models import EventMembership
    from teams.models import Team, TeamMember

    def make(event, captain=None, members=(), name=None):
        captain = captain or make_user()
        team = Team.objects.create(event=event, name=name or f"Team {Team.objects.count() + 1}", captain=captain)
        for person in (captain, *members):
            TeamMember.objects.create(team=team, user=person)
            EventMembership.objects.get_or_create(user=person, event=event, role=Role.PARTICIPANT)
        return team

    return make


@pytest.fixture
def client_for():
    """A logged-in Client for an existing user (via the real login form)."""

    def make(user, ip="10.0.0.9"):
        client = Client(REMOTE_ADDR=ip)
        assert client.post("/login", {"email": user.email, "password": PASSWORD}).status_code == 302
        client.user = user
        return client

    return make
