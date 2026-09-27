"""The role model, enforced at the server for every portal."""

import pytest
from django.test import Client

from accounts.roles import PORTAL_ACCESS, Role
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db

PORTALS = {
    "participant": "/participant/",
    "judge": "/judge/",
    "organizer": "/organizer/",
    "admin": "/admin/",
}

MATRIX = [
    (role, portal, role in PORTAL_ACCESS[portal])
    for role in Role.values
    for portal in PORTALS
]


@pytest.mark.parametrize("role, portal, allowed", MATRIX)
def test_portal_access_matrix_over_sessions(login_client, role, portal, allowed):
    client = login_client(role)
    response = client.get(PORTALS[portal])
    assert response.status_code == (200 if allowed else 403)


@pytest.mark.parametrize("role, portal, allowed", MATRIX)
def test_portal_access_matrix_over_bearer_tokens(make_user, role, portal, allowed):
    from accounts.models import ApiToken

    _, raw = ApiToken.issue(make_user(role=role), "t")
    response = Client().get(PORTALS[portal], HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        assert response.json()["error"] == "forbidden"


def test_judges_and_participants_are_kept_apart():
    assert PORTAL_ACCESS["judge"] == {Role.JUDGE}
    assert PORTAL_ACCESS["participant"] == {Role.PARTICIPANT}


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


def test_admin_creates_judge_account(login_client):
    client = login_client(Role.ADMIN)
    response = client.post(
        "/admin/accounts/new",
        {
            "name": "Grace Judge",
            "email": "grace@example.org",
            "role": "judge",
            "password1": "a-long-enough-pass",
            "password2": "a-long-enough-pass",
        },
    )
    assert response.status_code == 302
    from accounts.models import User

    assert User.objects.get(email="grace@example.org").role == Role.JUDGE
    assert AuditLog.objects.filter(action=AuditAction.ACCOUNT_CREATED).exists()


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER])
def test_only_admins_create_accounts(login_client, role):
    client = login_client(role)
    response = client.post(
        "/admin/accounts/new",
        {"name": "X", "email": "x@example.org", "role": "admin",
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


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER])
def test_database_admin_refuses_non_admins_without_looping(login_client, role):
    assert login_client(role).get("/admin/db/").status_code == 403


def test_database_admin_opens_for_admins(login_client):
    assert login_client(Role.ADMIN).get("/admin/db/").status_code == 200


def test_audit_log_is_read_only_even_for_admins(login_client):
    client = login_client(Role.ADMIN)
    row = AuditLog.objects.first()
    assert client.get("/admin/db/core/auditlog/").status_code == 200
    assert client.get("/admin/db/core/auditlog/add/").status_code == 403
    assert client.post(f"/admin/db/core/auditlog/{row.pk}/delete/", {"post": "yes"}).status_code == 403
    assert AuditLog.objects.filter(pk=row.pk).exists()
