"""Session management (list, revoke, password change) and Bearer tokens."""

import pytest
from django.test import Client

from accounts.models import ApiToken, UserSession, digest_token
from accounts.roles import Role
from conftest import PASSWORD
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


def second_login(user, ip="10.0.0.2"):
    client = Client(REMOTE_ADDR=ip, HTTP_USER_AGENT="Mozilla/5.0 (Macintosh; Mac OS X 14) Safari/605")
    assert client.post("/login", {"email": user.email, "password": PASSWORD}).status_code == 302
    return client


# --- sessions ----------------------------------------------------------------------------


def test_account_page_lists_every_live_session(login_client):
    laptop = login_client()
    phone = second_login(laptop.user)
    response = laptop.get("/account")
    assert response.status_code == 200
    assert len(response.context["sessions"]) == 2
    assert b"Firefox on Linux" in response.content and b"Safari on macOS" in response.content
    assert phone.get("/account").status_code == 200


def test_revoking_a_session_logs_that_browser_out(login_client):
    laptop = login_client()
    phone = second_login(laptop.user)
    phone_row = UserSession.objects.get(session_key=phone.session.session_key)
    response = laptop.post(f"/account/sessions/{phone_row.pk}/revoke")
    assert response.status_code == 302
    assert phone.get("/participant/").status_code == 302  # bounced to /login
    assert laptop.get("/participant/").status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.SESSION_REVOKED).exists()


def test_revoking_the_current_session_logs_out(login_client):
    client = login_client()
    row = UserSession.objects.get(session_key=client.session.session_key)
    client.post(f"/account/sessions/{row.pk}/revoke")
    assert client.get("/participant/").status_code == 302


def test_cannot_revoke_someone_elses_session(login_client):
    alice = login_client()
    bob = login_client()
    bob_row = UserSession.objects.get(user=bob.user)
    assert alice.post(f"/account/sessions/{bob_row.pk}/revoke").status_code == 404
    assert bob.get("/participant/").status_code == 200


def test_sign_out_other_sessions(login_client):
    laptop = login_client()
    phone = second_login(laptop.user)
    tablet = second_login(laptop.user, ip="10.0.0.3")
    laptop.post("/account/sessions/revoke-others")
    assert phone.get("/participant/").status_code == 302
    assert tablet.get("/participant/").status_code == 302
    assert laptop.get("/participant/").status_code == 200
    assert UserSession.objects.filter(user=laptop.user).count() == 1


def test_password_change_keeps_this_session_and_ends_the_others(login_client):
    laptop = login_client()
    phone = second_login(laptop.user)
    response = laptop.post(
        "/account/password",
        {"old_password": PASSWORD, "new_password1": "brand-new-pass-99", "new_password2": "brand-new-pass-99"},
    )
    assert response.status_code == 302
    assert laptop.get("/participant/").status_code == 200
    assert phone.get("/participant/").status_code == 302
    # The surviving session's metadata row followed the rotated key.
    assert UserSession.objects.filter(session_key=laptop.session.session_key).exists()
    assert Client().post("/login", {"email": laptop.user.email, "password": "brand-new-pass-99"}).status_code == 302


def test_password_change_needs_the_current_password(login_client):
    client = login_client()
    response = client.post(
        "/account/password",
        {"old_password": "wrong", "new_password1": "brand-new-pass-99", "new_password2": "brand-new-pass-99"},
    )
    assert response.status_code == 400
    client.user.refresh_from_db()
    assert client.user.check_password(PASSWORD)


def test_session_post_without_csrf_token_is_refused(make_user):
    user = make_user()
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post("/account/sessions/revoke-others").status_code == 403


# --- bearer tokens -----------------------------------------------------------------------


def test_created_token_is_shown_once_and_stored_only_as_a_digest(login_client):
    client = login_client()
    client.post("/account/tokens", {"name": "scripts"})
    first = client.get("/account")
    raw = first.context["new_token"]["raw"]
    assert raw.startswith("dfk_") and raw.encode() in first.content
    token = ApiToken.objects.get(user=client.user)
    assert token.digest == digest_token(raw) and raw not in token.digest
    second = client.get("/account")
    assert second.context["new_token"] is None
    assert raw.encode() not in second.content


def test_bearer_token_authenticates_api_calls(make_user):
    user = make_user(role=Role.JUDGE)
    _, raw = ApiToken.issue(user, "t")
    response = Client().get("/api/me", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 200
    assert response.json() == {
        "id": user.pk, "email": user.email, "name": user.name, "role": "judge",
        "portal": "/judge/", "auth": "bearer",
    }
    assert ApiToken.objects.get(user=user).last_used_at is not None


def test_session_authenticates_api_calls_too(login_client):
    response = login_client(Role.ORGANIZER).get("/api/me")
    assert response.json()["auth"] == "session"


def test_api_without_credentials_is_401_json():
    response = Client().get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"
    assert response["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer nope", "Bearer ", "bearer dfk_forged"])
def test_bad_token_is_a_hard_401_even_on_public_pages(header):
    response = Client().get("/", HTTP_AUTHORIZATION=header)
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_revoked_token_stops_working(login_client):
    client = login_client()
    token, raw = ApiToken.issue(client.user, "t")
    client.post(f"/account/tokens/{token.pk}/revoke")
    assert Client().get("/api/me", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 401


def test_token_of_deactivated_user_stops_working(make_user):
    user = make_user()
    _, raw = ApiToken.issue(user, "t")
    user.is_active = False
    user.save()
    assert Client().get("/api/me", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 401


def test_cannot_revoke_someone_elses_token(login_client, make_user):
    client = login_client()
    other_token, raw = ApiToken.issue(make_user(), "theirs")
    assert client.post(f"/account/tokens/{other_token.pk}/revoke").status_code == 404
    assert Client().get("/api/me", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 200


def test_bearer_requests_skip_csrf_but_sessions_do_not(make_user):
    """Browsers never add an Authorization header by themselves, so a Bearer request cannot be
    forged cross-site; cookie requests keep the CSRF check."""
    user = make_user()
    _, raw = ApiToken.issue(user, "t")
    client = Client(enforce_csrf_checks=True)
    response = client.post("/account/tokens", {"name": "via-api"}, HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 302
    assert ApiToken.objects.filter(user=user, name="via-api").exists()


def test_bearer_ignores_a_session_cookie_for_someone_else(login_client, make_user):
    client = login_client(Role.ADMIN)
    _, raw = ApiToken.issue(make_user(role=Role.PARTICIPANT), "t")
    response = client.get("/api/me", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.json()["role"] == "participant"
