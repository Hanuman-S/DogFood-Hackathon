import pytest
from django.test import Client

from accounts.models import User
from accounts.roles import Role

PASSWORD = "correct-horse-battery"


@pytest.fixture
def make_user(db):
    def make(role=Role.PARTICIPANT, email=None, password=PASSWORD, name="Test User"):
        email = email or f"{role}-{User.objects.count() + 1}@example.org"
        return User.objects.create_user(email, password, name=name, role=role)

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
    from events.models import Event, EventOrganizer

    def make(slug=None, organizer=None, published=True, max_team_size=4, **dates):
        now = timezone.now()
        organizer = organizer or make_user(role=Role.ORGANIZER)
        event = Event.objects.create(
            slug=slug or f"event-{Event.objects.count() + 1}",
            name="Test Hack",
            starts_at=dates.get("starts_at", now - timedelta(days=1)),
            submissions_open_at=dates.get("submissions_open_at", now - timedelta(days=1)),
            submissions_close_at=dates.get("submissions_close_at", now + timedelta(days=2)),
            judging_ends_at=dates.get("judging_ends_at", now + timedelta(days=5)),
            max_team_size=max_team_size,
            is_published=published,
            created_by=organizer,
        )
        EventOrganizer.objects.create(event=event, user=organizer, added_by=organizer)
        event.organizer = organizer
        return event

    return make


@pytest.fixture
def make_team(make_user):
    from teams.models import Team, TeamMember

    def make(event, captain=None, members=(), name=None):
        captain = captain or make_user()
        team = Team.objects.create(event=event, name=name or f"Team {Team.objects.count() + 1}", captain=captain)
        TeamMember.objects.create(team=team, user=captain)
        for member in members:
            TeamMember.objects.create(team=team, user=member)
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
