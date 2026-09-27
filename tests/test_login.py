"""Login, logout, sign-up, throttling and redirects."""

from datetime import timedelta

import pytest
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.utils import timezone

from accounts.models import User, UserSession
from accounts.roles import Role
from conftest import PASSWORD
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


def post_login(client, email, password=PASSWORD, **extra):
    return client.post("/login", {"email": email, "password": password, **extra})


# --- login -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role, portal",
    [
        (Role.PARTICIPANT, "/participant/"),
        (Role.JUDGE, "/judge/"),
        (Role.ORGANIZER, "/organizer/"),
        (Role.ADMIN, "/admin/"),
    ],
)
def test_login_lands_on_the_role_portal(make_user, role, portal):
    user = make_user(role=role)
    response = post_login(Client(), user.email)
    assert response.status_code == 302
    assert response["Location"] == portal


def test_login_is_case_insensitive_on_email(make_user):
    user = make_user(email="Mixed.Case@Example.org")
    assert user.email == "mixed.case@example.org"
    assert post_login(Client(), "MIXED.case@EXAMPLE.ORG").status_code == 302


def test_wrong_password_and_unknown_email_look_identical(make_user):
    user = make_user()
    wrong = post_login(Client(), user.email, "not-the-password")
    unknown = post_login(Client(), "nobody@example.org", "not-the-password")
    assert wrong.status_code == unknown.status_code == 200
    assert b"invalid email or password" in wrong.content
    assert b"invalid email or password" in unknown.content
    assert AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).count() == 2


def test_inactive_user_cannot_log_in(make_user):
    user = make_user()
    user.is_active = False
    user.save()
    assert post_login(Client(), user.email).status_code == 200


def test_login_rotates_the_session_key(make_user):
    """No session fixation: a key planted before login is useless after it."""
    user = make_user()
    client = Client()
    client.get("/login")
    client.session.save()
    before = client.session.session_key
    post_login(client, user.email)
    assert client.session.session_key != before


def test_login_records_a_user_session_and_audit_row(make_user):
    user = make_user()
    client = Client(REMOTE_ADDR="10.1.2.3", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/140")
    post_login(client, user.email)
    row = UserSession.objects.get(user=user)
    assert row.session_key == client.session.session_key
    assert row.ip == "10.1.2.3"
    assert row.device == "Chrome on Windows"
    assert AuditLog.objects.filter(action=AuditAction.LOGIN_OK, actor=user).exists()


def test_without_remember_the_cookie_dies_with_the_browser(make_user):
    user = make_user()
    client = Client()
    post_login(client, user.email)
    assert client.session.get_expire_at_browser_close()


def test_with_remember_the_session_persists(make_user):
    user = make_user()
    client = Client()
    post_login(client, user.email, remember="on")
    assert not client.session.get_expire_at_browser_close()


def test_session_cookie_is_httponly_and_samesite(make_user):
    user = make_user()
    response = post_login(Client(), user.email, remember="on")
    cookie = response.cookies["dogfood_session"]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"


def test_next_parameter_is_followed_when_local(make_user):
    user = make_user(role=Role.JUDGE)
    response = post_login(Client(), user.email, next="/account")
    assert response["Location"] == "/account"


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "javascript:alert(1)"])
def test_next_parameter_cannot_redirect_off_site(make_user, target):
    user = make_user(role=Role.JUDGE)
    response = post_login(Client(), user.email, next=target)
    assert response["Location"] == "/judge/"


def test_logged_in_user_visiting_login_goes_to_portal(login_client):
    client = login_client(Role.ORGANIZER)
    response = client.get("/login")
    assert response.status_code == 302 and response["Location"] == "/organizer/"


# --- throttle ----------------------------------------------------------------------------


def test_five_failures_throttle_even_the_right_password(make_user):
    user = make_user()
    client = Client(REMOTE_ADDR="10.9.9.9")
    for _ in range(5):
        post_login(client, user.email, "wrong-password")
    response = post_login(client, user.email)  # correct password, still refused
    assert response.status_code == 429
    assert b"too many failed attempts" in response.content
    assert "_auth_user_id" not in client.session
    assert AuditLog.objects.filter(action=AuditAction.LOGIN_THROTTLED).count() == 1


def test_throttle_is_per_ip_so_a_victim_is_not_locked_out(make_user):
    user = make_user()
    attacker = Client(REMOTE_ADDR="10.6.6.6")
    for _ in range(6):
        post_login(attacker, user.email, "wrong-password")
    victim = Client(REMOTE_ADDR="10.1.1.1")
    assert post_login(victim, user.email).status_code == 302


def test_throttle_window_expires(make_user):
    user = make_user()
    client = Client(REMOTE_ADDR="10.9.9.8")
    for _ in range(5):
        post_login(client, user.email, "wrong-password")
    AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).update(
        created_at=timezone.now() - timedelta(minutes=16)
    )
    assert post_login(client, user.email).status_code == 302


def test_success_resets_the_per_account_counter(make_user):
    user = make_user()
    client = Client(REMOTE_ADDR="10.9.9.7")
    for _ in range(4):
        post_login(client, user.email, "wrong-password")
    assert post_login(client, user.email).status_code == 302
    client.post("/logout")
    for _ in range(4):
        post_login(client, user.email, "wrong-password")
    assert post_login(client, user.email).status_code == 302


@override_settings(LOGIN_IP_FAILURE_LIMIT=3)
def test_one_ip_spraying_many_accounts_is_throttled(make_user):
    client = Client(REMOTE_ADDR="10.5.5.5")
    for i in range(3):
        post_login(client, f"victim{i}@example.org", "guess")
    target = make_user()
    assert post_login(client, target.email).status_code == 429


# --- logout ------------------------------------------------------------------------------


def test_logout_requires_post(login_client):
    client = login_client()
    assert client.get("/logout").status_code == 405
    assert "_auth_user_id" in client.session


def test_logout_ends_the_session_server_side(login_client):
    client = login_client()
    key = client.session.session_key
    response = client.post("/logout")
    assert response.status_code == 302
    assert not Session.objects.filter(session_key=key).exists()
    assert not UserSession.objects.filter(session_key=key).exists()
    assert AuditLog.objects.filter(action=AuditAction.LOGOUT).exists()
    assert client.get("/participant/").status_code == 302  # back to the login page


def test_logout_is_csrf_protected():
    client = Client(enforce_csrf_checks=True)
    assert client.post("/logout").status_code == 403


# --- sign-up -----------------------------------------------------------------------------


def signup(client, **overrides):
    data = {
        "name": "New Person",
        "email": "new@example.org",
        "password1": "a-long-enough-pass",
        "password2": "a-long-enough-pass",
    }
    data.update(overrides)
    return client.post("/signup", data)


def test_signup_creates_a_logged_in_participant():
    client = Client()
    response = signup(client)
    assert response.status_code == 302 and response["Location"] == "/participant/"
    user = User.objects.get(email="new@example.org")
    assert user.role == Role.PARTICIPANT
    assert client.session["_auth_user_id"] == str(user.pk)
    assert AuditLog.objects.filter(action=AuditAction.SIGNUP, actor=user).exists()


def test_signup_cannot_choose_a_role():
    signup(Client(), role="admin")
    assert User.objects.get(email="new@example.org").role == Role.PARTICIPANT


def test_signup_rejects_a_case_twin_of_an_existing_email(make_user):
    make_user(email="taken@example.org")
    response = signup(Client(), email="TAKEN@example.org")
    assert response.status_code == 400
    assert User.objects.filter(email__iexact="taken@example.org").count() == 1


@pytest.mark.parametrize("password", ["short", "1234567890123", "password1234"])
def test_signup_rejects_weak_passwords(password):
    response = signup(Client(), password1=password, password2=password)
    assert response.status_code == 400
    assert not User.objects.filter(email="new@example.org").exists()


def test_signup_rejects_mismatched_passwords():
    assert signup(Client(), password2="something-else-entirely").status_code == 400


def test_passwords_are_stored_hashed(make_user):
    user = make_user()
    assert PASSWORD not in user.password
    assert user.check_password(PASSWORD)


@override_settings(ALLOW_SIGNUP=False)
def test_signup_can_be_closed():
    assert Client().get("/signup").status_code == 403
    assert signup(Client()).status_code == 403
    assert not User.objects.exists()
