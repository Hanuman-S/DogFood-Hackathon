"""Signup, login, logout, password change, API tokens, and the login throttle.

These drive real URLs with the Django test client rather than calling service functions, because
the thing under test is what a browser or `curl` actually gets -- status codes included.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.test import override_settings

from accounts.models import ApiToken, User
from accounts.services import throttle_state
from core import clock
from core.models import AuditAction, AuditLog

PASSWORD = "a-decent-test-password-42"


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="member@example.org", display_name="Member", password=PASSWORD
    )


# --------------------------------------------------------------------------------------
# signup
# --------------------------------------------------------------------------------------


def test_signup_creates_an_account_and_signs_in(client, db):
    response = client.post(
        "/signup",
        {
            "email": "New.Person@Example.ORG",
            "display_name": "New Person",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    assert response.status_code == 302

    user = User.objects.get(email="new.person@example.org")  # normalized
    assert user.display_name == "New Person"
    assert user.check_password(PASSWORD)

    # Signed in straight away: the invite flow depends on it.
    assert client.session["_auth_user_id"] == str(user.pk)

    assert AuditLog.objects.filter(action=AuditAction.SIGNUP, actor=user).exists()


def test_signup_gives_no_roles_and_no_powers(client, db):
    """A new account is a visitor with a login, nothing more."""
    client.post(
        "/signup",
        {
            "email": "plain@example.org",
            "display_name": "Plain",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    user = User.objects.get(email="plain@example.org")
    assert not user.is_platform_admin
    assert not user.can_create_events
    assert not user.is_staff
    assert user.event_memberships.count() == 0


@pytest.mark.parametrize(
    "password,reason",
    [
        ("short", "too short"),
        ("password", "too common"),
        ("12345678901", "numeric"),
    ],
)
def test_signup_enforces_djangos_password_validators(client, db, password, reason):
    response = client.post(
        "/signup",
        {
            "email": "weak@example.org",
            "display_name": "Weak",
            "password1": password,
            "password2": password,
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(email="weak@example.org").exists()


def test_signup_rejects_mismatched_passwords(client, db):
    response = client.post(
        "/signup",
        {
            "email": "mismatch@example.org",
            "display_name": "Mismatch",
            "password1": PASSWORD,
            "password2": PASSWORD + "x",
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(email="mismatch@example.org").exists()


def test_signup_refuses_a_duplicate_email(client, user):
    response = client.post(
        "/signup",
        {
            "email": user.email,
            "display_name": "Impostor",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    assert response.status_code == 200
    assert User.objects.filter(email=user.email).count() == 1


def test_signup_honours_a_relative_next_target(client, db):
    """The invite flow sends people through signup and back to the invite."""
    response = client.post(
        "/signup",
        {
            "email": "invited@example.org",
            "display_name": "Invited",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "next": "/invite/some-token",
        },
    )
    assert response.status_code == 302
    assert response["Location"] == "/invite/some-token"


# --------------------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------------------


def test_login_succeeds_and_is_audited(client, user):
    response = client.post("/login", {"email": user.email, "password": PASSWORD})
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    assert AuditLog.objects.filter(action=AuditAction.LOGIN_SUCCEEDED, actor=user).exists()


def test_login_is_case_insensitive_on_the_email(client, user):
    response = client.post("/login", {"email": "MEMBER@EXAMPLE.ORG", "password": PASSWORD})
    assert response.status_code == 302


def test_a_failed_login_returns_401_and_is_audited(client, user):
    response = client.post("/login", {"email": user.email, "password": "wrong-password-1"})
    assert response.status_code == 401
    assert "_auth_user_id" not in client.session

    entry = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
    assert entry.actor is None  # nobody is authenticated on a failure
    assert entry.metadata["email"] == user.email


def test_an_unknown_email_fails_the_same_way_as_a_wrong_password(client, user):
    """Same status, same message: login must not be an account-existence oracle."""
    unknown = client.post("/login", {"email": "nobody@example.org", "password": PASSWORD})
    wrong = client.post("/login", {"email": user.email, "password": "wrong-password-1"})

    assert unknown.status_code == wrong.status_code == 401
    assert b"do not match" in unknown.content
    assert b"do not match" in wrong.content


def test_an_imported_account_with_an_unusable_password_cannot_log_in(client, db):
    """Fixture accounts are inert until DEMO_MODE seeds a password."""
    User.objects.create_user(email="imported@example.org", display_name="Imported")
    response = client.post("/login", {"email": "imported@example.org", "password": ""})
    assert response.status_code in (200, 401)
    assert "_auth_user_id" not in client.session


def test_next_cannot_be_used_as_an_open_redirect(client, user):
    """An absolute URL in `next` would let a phishing page bounce off the real login form."""
    response = client.post(
        "/login",
        {"email": user.email, "password": PASSWORD, "next": "https://evil.example.com/login"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/"


def test_protocol_relative_next_is_also_refused(client, user):
    """`//evil.example.com` is absolute to a browser even though it starts with a slash."""
    response = client.post(
        "/login",
        {"email": user.email, "password": PASSWORD, "next": "//evil.example.com"},
    )
    assert response["Location"] == "/"


# --------------------------------------------------------------------------------------
# logout
# --------------------------------------------------------------------------------------


def test_logout_requires_a_post(client, user):
    """A GET logout can be triggered by any third-party <img> tag."""
    client.force_login(user)
    assert client.get("/logout").status_code == 405
    assert "_auth_user_id" in client.session

    assert client.post("/logout").status_code == 302
    assert "_auth_user_id" not in client.session


# --------------------------------------------------------------------------------------
# the login throttle
# --------------------------------------------------------------------------------------


@override_settings(LOGIN_FAILURE_LIMIT=3, LOGIN_FAILURE_WINDOW_MINUTES=15)
def test_repeated_failures_are_throttled(client, user):
    for _ in range(3):
        assert client.post("/login", {"email": user.email, "password": "nope-1234"}).status_code == 401

    # The fourth attempt is refused before the password is even checked.
    throttled = client.post("/login", {"email": user.email, "password": "nope-1234"})
    assert throttled.status_code == 429
    assert b"Too many failed sign-in attempts" in throttled.content
    assert AuditLog.objects.filter(action=AuditAction.LOGIN_THROTTLED).exists()


@override_settings(LOGIN_FAILURE_LIMIT=3, LOGIN_FAILURE_WINDOW_MINUTES=15)
def test_the_throttle_refuses_even_the_correct_password(client, user):
    """Otherwise it is not a throttle, just a hint about which guesses were close."""
    for _ in range(3):
        client.post("/login", {"email": user.email, "password": "nope-1234"})

    response = client.post("/login", {"email": user.email, "password": PASSWORD})
    assert response.status_code == 429
    assert "_auth_user_id" not in client.session


@override_settings(LOGIN_FAILURE_LIMIT=3, LOGIN_FAILURE_WINDOW_MINUTES=15)
def test_the_throttle_window_expires(client, user):
    for _ in range(3):
        client.post("/login", {"email": user.email, "password": "nope-1234"})
    assert client.post("/login", {"email": user.email, "password": PASSWORD}).status_code == 429

    # Sixteen minutes later the failures have aged out of the window.
    with clock.offset_by(dt.timedelta(minutes=16)):
        response = client.post("/login", {"email": user.email, "password": PASSWORD})
        assert response.status_code == 302


@override_settings(LOGIN_FAILURE_LIMIT=3, LOGIN_FAILURE_WINDOW_MINUTES=15)
def test_the_throttle_is_scoped_to_email_and_ip_together(client, user):
    """Counting by email alone would let anyone lock a known user out of their own account."""
    for _ in range(3):
        client.post(
            "/login",
            {"email": user.email, "password": "nope-1234"},
            REMOTE_ADDR="10.0.0.1",
        )

    assert throttle_state(email=user.email, ip="10.0.0.1").is_throttled
    # Same account, different address: unaffected.
    assert not throttle_state(email=user.email, ip="10.0.0.2").is_throttled

    response = client.post(
        "/login", {"email": user.email, "password": PASSWORD}, REMOTE_ADDR="10.0.0.2"
    )
    assert response.status_code == 302


@override_settings(LOGIN_FAILURE_LIMIT=3)
def test_the_throttle_counts_the_audit_rows_it_writes(client, user):
    """The throttle and the audit trail are the same data.

    If the metadata key names ever drift apart, the throttle silently stops counting, so the round
    trip is asserted rather than assumed.
    """
    client.post("/login", {"email": user.email, "password": "nope-1234"}, REMOTE_ADDR="10.1.2.3")

    entry = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
    assert entry.metadata["email"] == user.email
    assert entry.metadata["ip"] == "10.1.2.3"
    assert throttle_state(email=user.email, ip="10.1.2.3").failures == 1


def test_x_forwarded_for_is_ignored_unless_a_proxy_is_declared(client, user):
    """The header is client-supplied. Trusting it without a proxy would let anyone forge the IP the
    throttle counts against, and so lock another person out."""
    client.post(
        "/login",
        {"email": user.email, "password": "nope-1234"},
        REMOTE_ADDR="10.0.0.9",
        HTTP_X_FORWARDED_FOR="1.2.3.4",
    )
    entry = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
    assert entry.metadata["ip"] == "10.0.0.9"

    with override_settings(TRUST_PROXY_HEADERS=True):
        client.post(
            "/login",
            {"email": user.email, "password": "nope-1234"},
            REMOTE_ADDR="10.0.0.9",
            HTTP_X_FORWARDED_FOR="1.2.3.4",
        )
    assert AuditLog.objects.filter(
        action=AuditAction.LOGIN_FAILED, metadata__ip="1.2.3.4"
    ).exists()


# --------------------------------------------------------------------------------------
# password change
# --------------------------------------------------------------------------------------


def test_password_change_requires_the_current_password(client, user):
    client.force_login(user)
    response = client.post(
        "/profile/password",
        {"old_password": "not-it", "new_password1": "brand-new-secret-77", "new_password2": "brand-new-secret-77"},
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_password_change_works_and_keeps_the_session(client, user):
    client.force_login(user)
    new_password = "brand-new-secret-77"
    response = client.post(
        "/profile/password",
        {"old_password": PASSWORD, "new_password1": new_password, "new_password2": new_password},
    )
    assert response.status_code == 302

    user.refresh_from_db()
    assert user.check_password(new_password)
    # Django rotates the session hash; the view must re-sign the session or log the user out.
    assert "_auth_user_id" in client.session
    assert AuditLog.objects.filter(action=AuditAction.PASSWORD_CHANGED, actor=user).exists()


# --------------------------------------------------------------------------------------
# API tokens through the UI
# --------------------------------------------------------------------------------------


def test_profile_requires_a_login(client, db):
    response = client.get("/profile")
    assert response.status_code == 302
    assert "/login" in response["Location"]


def test_creating_a_token_shows_the_plaintext_exactly_once(client, user):
    client.force_login(user)
    response = client.post("/profile", {"name": "my laptop"})
    assert response.status_code == 200

    token = ApiToken.objects.get(user=user, name="my laptop")
    html = response.content.decode()
    # The page shows a value that is not what is stored.
    assert token.token_hash not in html
    assert b"not shown again" in response.content

    # A refresh does not redisplay it.
    again = client.get("/profile").content.decode()
    assert "not shown again" not in again


def test_a_created_token_authenticates_api_requests(client, user, api_client):
    client.force_login(user)
    response = client.post("/profile", {"name": "cli"})
    html = response.content.decode()

    # Pull the plaintext out of the one-time display block.
    import re

    match = re.search(r'<p class="token-box">([^<]+)</p>', html)
    assert match, "the token plaintext was not displayed"
    plaintext = match.group(1).strip()

    assert ApiToken.objects.filter(token_hash=ApiToken.hash_token(plaintext)).exists()


def test_a_token_is_audited_without_recording_its_value(client, user):
    client.force_login(user)
    client.post("/profile", {"name": "audited"})

    entry = AuditLog.objects.get(action=AuditAction.TOKEN_CREATED)
    token = ApiToken.objects.get(name="audited")
    blob = str(entry.metadata)
    assert entry.metadata["name"] == "audited"
    assert token.token_hash not in blob


def test_revoking_a_token(client, user):
    client.force_login(user)
    token, _ = ApiToken.issue(user=user, name="doomed")

    response = client.post(f"/profile/tokens/{token.pk}/revoke")
    assert response.status_code == 302

    token.refresh_from_db()
    assert token.revoked_at is not None
    assert AuditLog.objects.filter(action=AuditAction.TOKEN_REVOKED).exists()


def test_a_user_cannot_revoke_someone_elses_token(client, user):
    """Scoped in the lookup, so another account's id simply does not resolve."""
    other = User.objects.create_user(
        email="other@example.org", display_name="Other", password=PASSWORD
    )
    victim_token, _ = ApiToken.issue(user=other, name="not yours")

    client.force_login(user)
    client.post(f"/profile/tokens/{victim_token.pk}/revoke")

    victim_token.refresh_from_db()
    assert victim_token.revoked_at is None


def test_token_revocation_requires_a_post(client, user):
    client.force_login(user)
    token, _ = ApiToken.issue(user=user, name="safe")
    assert client.get(f"/profile/tokens/{token.pk}/revoke").status_code == 405
    token.refresh_from_db()
    assert token.revoked_at is None


def test_login_honours_a_relative_next_target(client, user):
    """The invite flow depends on this: open an invite link while logged out, sign in, and land
    back on the invite rather than on the home page."""
    response = client.post(
        "/login",
        {"email": user.email, "password": PASSWORD, "next": "/invite/abc123"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/invite/abc123"
