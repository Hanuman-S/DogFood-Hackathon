"""The role model, enforced at the server: portal gates, then roles *per event*."""

import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from accounts.roles import ADMIN, Role, can_compete_in, portals_of, roles_in
from core.models import AuditAction, AuditLog
from events.models import EventMembership

pytestmark = pytest.mark.django_db

PORTALS = {
    "participant": "/participant/",
    "judge": "/judge/",
    "organizer": "/organizer/",
    "admin": "/admin/",
}

# account kind (see conftest.make_user) -> portals it may enter. Roles are per event, so a judge
# of one event may still compete in another: the participant portal is open to every account
# except platform admins, who run every event and so can compete in none.
ALLOWED = {
    Role.PARTICIPANT: {"participant"},
    Role.JUDGE: {"participant", "judge"},
    Role.ORGANIZER: {"participant", "organizer"},
    ADMIN: {"organizer", "admin"},
}

MATRIX = [(kind, portal, portal in allowed) for kind, allowed in ALLOWED.items() for portal in PORTALS]


@pytest.mark.parametrize("kind, portal, allowed", MATRIX)
def test_portal_access_matrix_over_sessions(login_client, kind, portal, allowed):
    client = login_client(kind)
    response = client.get(PORTALS[portal])
    assert response.status_code == (200 if allowed else 403)


@pytest.mark.parametrize("kind, portal, allowed", MATRIX)
def test_portal_access_matrix_over_bearer_tokens(make_user, kind, portal, allowed):
    from accounts.models import ApiToken

    _, raw = ApiToken.issue(make_user(role=kind), "t")
    response = Client().get(PORTALS[portal], HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        assert response.json()["error"] == "forbidden"


def test_login_lands_on_the_most_powerful_portal(make_user):
    assert portals_of(make_user(role=ADMIN))[0] == "admin"
    assert portals_of(make_user(role=Role.ORGANIZER))[0] == "organizer"
    assert portals_of(make_user(role=Role.JUDGE))[0] == "judge"
    assert portals_of(make_user())[0] == "participant"


@pytest.mark.parametrize("path", PORTALS.values())
def test_visitors_are_sent_to_login(path):
    response = Client().get(path)
    assert response.status_code == 302
    assert response["Location"] == f"/login?next={path}"


def test_refused_access_is_audited(login_client):
    client = login_client(Role.PARTICIPANT)
    client.get("/judge/")
    row = AuditLog.objects.get(action=AuditAction.ACCESS_DENIED)
    assert row.subject == "judge"
    assert row.actor == client.user


def test_public_home_is_open_to_visitors():
    response = Client().get("/")
    assert response.status_code == 200
    assert b"DOGFOOD" in response.content


# --- roles are per event ----------------------------------------------------------------------


def test_an_organizer_cannot_reach_another_organizers_event(make_event, client_for):
    mine, theirs = make_event(), make_event()
    client = client_for(mine.organizer)
    assert client.get(f"/organizer/events/{mine.slug}/").status_code == 200
    assert client.get(f"/organizer/events/{theirs.slug}/").status_code == 404
    response = client.post(f"/organizer/events/{theirs.slug}/publish", {"publish": "0"})
    assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.is_published


def test_a_judge_of_one_event_may_compete_in_another(make_event, make_user, client_for):
    judged, other = make_event(slug="judged"), make_event(slug="other")
    judge = make_user()
    EventMembership.objects.create(user=judge, event=judged, role=Role.JUDGE)
    client = client_for(judge)

    from teams.models import TeamMember

    client.post(f"/participant/events/{judged.slug}/team", {"name": "Conflicted"})
    assert not TeamMember.objects.filter(user=judge, event=judged).exists()
    assert roles_in(judge, judged) == {Role.JUDGE}

    client.post(f"/participant/events/{other.slug}/team", {"name": "Fair and square"})
    assert roles_in(judge, other) == {Role.PARTICIPANT}


def test_staff_cannot_compete_in_their_own_event(make_event, make_user):
    event = make_event()
    assert can_compete_in(event.organizer, event)
    judge = make_user()
    EventMembership.objects.create(user=judge, event=event, role=Role.JUDGE)
    assert can_compete_in(judge, event)
    assert can_compete_in(make_user(role=ADMIN), event)
    assert can_compete_in(make_user(), event) == ""


def test_the_database_refuses_a_competitor_who_is_also_staff(make_event, make_team):
    """The exclusion constraint, independently of the services."""
    event = make_event()
    team = make_team(event)
    with pytest.raises(IntegrityError), transaction.atomic():
        EventMembership.objects.create(user=team.captain, event=event, role=Role.JUDGE)


def test_judge_and_organizer_together_is_allowed(make_event):
    event = make_event()
    EventMembership.objects.create(user=event.organizer, event=event, role=Role.JUDGE)
    assert roles_in(event.organizer, event) == {Role.ORGANIZER, Role.JUDGE}


def test_organizer_makes_someone_a_judge(make_event, make_user, client_for):
    event = make_event()
    person = make_user()
    response = client_for(event.organizer).post(
        f"/organizer/events/{event.slug}/judges", {"email": person.email}
    )
    assert response.status_code == 302
    assert roles_in(person, event) == {Role.JUDGE}
    assert AuditLog.objects.filter(action=AuditAction.JUDGE_ADDED).exists()


def test_a_competitor_cannot_be_made_a_judge_of_the_same_event(make_event, make_team, client_for):
    event = make_event()
    team = make_team(event)
    response = client_for(event.organizer).post(
        f"/organizer/events/{event.slug}/judges", {"email": team.captain.email}
    )
    assert response.status_code == 400
    assert b"conflict of interest" in response.content
    assert roles_in(team.captain, event) == {Role.PARTICIPANT}


def test_leaving_a_team_ends_the_participant_role(make_event, make_team, client_for):
    event = make_event()
    team = make_team(event)
    client_for(team.captain).post(f"/participant/teams/{team.pk}/leave")
    assert roles_in(team.captain, event) == frozenset()


def test_co_organizers_cannot_create_events_without_the_flag(make_event, make_user, client_for):
    event = make_event()
    helper = make_user()
    EventMembership.objects.create(user=helper, event=event, role=Role.ORGANIZER)
    client = client_for(helper)
    assert client.get("/organizer/").status_code == 200
    assert client.get("/organizer/events/new").status_code == 403


# --- accounts ------------------------------------------------------------------------------------


def test_admin_creates_an_account_with_platform_flags(login_client):
    client = login_client(ADMIN)
    response = client.post(
        "/admin/accounts/new",
        {
            "name": "Grace Organizer",
            "email": "grace@example.org",
            "password1": "a-long-enough-pass",
            "password2": "a-long-enough-pass",
            "can_create_events": "on",
        },
    )
    assert response.status_code == 302
    from accounts.models import User

    grace = User.objects.get(email="grace@example.org")
    assert grace.can_create_events and not grace.is_platform_admin
    assert AuditLog.objects.filter(action=AuditAction.ACCOUNT_CREATED).exists()


@pytest.mark.parametrize("kind", [Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER])
def test_only_admins_create_accounts(login_client, kind):
    client = login_client(kind)
    response = client.post(
        "/admin/accounts/new",
        {"name": "X", "email": "x@example.org", "is_platform_admin": "on",
         "password1": "a-long-enough-pass", "password2": "a-long-enough-pass"},
    )
    assert response.status_code == 403
    from accounts.models import User

    assert not User.objects.filter(email="x@example.org").exists()


# --- the database admin ------------------------------------------------------------------


def test_database_admin_redirects_visitors_to_our_login():
    response = Client().get("/admin/db/")
    assert response.status_code == 302
    assert response["Location"].startswith("/login?next=")


@pytest.mark.parametrize("kind", [Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER])
def test_database_admin_refuses_non_admins_without_looping(login_client, kind):
    assert login_client(kind).get("/admin/db/").status_code == 403


def test_database_admin_opens_for_admins(login_client):
    assert login_client(ADMIN).get("/admin/db/").status_code == 200


def test_audit_log_is_read_only_even_for_admins(login_client):
    client = login_client(ADMIN)
    row = AuditLog.objects.first()
    assert client.get("/admin/db/core/auditlog/").status_code == 200
    assert client.get("/admin/db/core/auditlog/add/").status_code == 403
    assert client.post(f"/admin/db/core/auditlog/{row.pk}/delete/", {"post": "yes"}).status_code == 403
    assert AuditLog.objects.filter(pk=row.pk).exists()
