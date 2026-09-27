"""`core.authentication.BearerTokenAuthentication`.

This class is load-bearing for the acceptance checker: the checker attaches exactly one header, so
it can send `Authorization: Bearer <token>` but never a CSRF token as well. If bearer auth required
CSRF, the closed-event check would be testing CSRF rather than the deadline.

The tests drive a throwaway view declared here rather than a production route, because no `/api/`
endpoint exists until phase 5 and inventing one just to be testable would be the wrong order of
operations. Phase 5 adds the same assertions against the real submit endpoint.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient

from accounts.models import ApiToken, User
from core import clock

# --- the throwaway endpoint ------------------------------------------------------------
#
# Deliberately requires authentication and accepts POST, so the tests can prove that a bearer
# token alone is sufficient for a write.


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def whoami(request):
    return Response({"email": request.user.email, "method": request.method})


urlpatterns = [path("probe", whoami)]

# Points the test client at this module's URLconf instead of the project's.
pytestmark = pytest.mark.urls("tests.test_bearer_auth")


@pytest.fixture
def user(db):
    return User.objects.create_user(email="api@example.org", display_name="API User")


@pytest.fixture
def token(user):
    """Returns `(instance, plaintext)`."""
    return ApiToken.issue(user=user, name="test token")


@pytest.fixture
def client():
    return APIClient()


def auth(plaintext: str) -> dict:
    """Exactly what the acceptance checker sends: one header, nothing else."""
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}


# --------------------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------------------


def test_a_valid_bearer_token_authenticates(client, token, user):
    instance, plaintext = token
    response = client.get("/probe", **auth(plaintext))
    assert response.status_code == 200
    assert response.json()["email"] == user.email


def test_a_bearer_token_authorizes_a_write_with_no_csrf_token(client, token, user):
    """The property the acceptance checker depends on.

    A bearer token is not an ambient credential -- a third-party page cannot make a browser attach
    it -- so CSRF protection is not what defends this path, and requiring a CSRF token would make
    the API unusable by any non-browser client.
    """
    instance, plaintext = token
    # `enforce_csrf_checks=True` makes the client behave like a browser with no token supplied.
    strict = APIClient(enforce_csrf_checks=True)
    response = strict.post("/probe", {"anything": "goes"}, format="json", **auth(plaintext))
    assert response.status_code == 200
    assert response.json()["method"] == "POST"


def test_the_scheme_is_case_insensitive(client, token):
    _, plaintext = token
    assert client.get("/probe", HTTP_AUTHORIZATION=f"bearer {plaintext}").status_code == 200
    assert client.get("/probe", HTTP_AUTHORIZATION=f"BEARER {plaintext}").status_code == 200


def test_using_a_token_records_when_it_was_last_used(client, token):
    instance, plaintext = token
    assert instance.last_used_at is None

    client.get("/probe", **auth(plaintext))

    instance.refresh_from_db()
    assert instance.last_used_at is not None
    # Recorded against the injectable clock, like every other timestamp.
    assert instance.last_used_at <= clock.now()


def test_last_used_does_not_disturb_the_creation_timestamp(client, token):
    instance, plaintext = token
    created = instance.created_at
    client.get("/probe", **auth(plaintext))
    instance.refresh_from_db()
    assert instance.created_at == created


# --------------------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------------------


def test_no_credentials_is_401(client, db):
    response = client.get("/probe")
    assert response.status_code == 401
    # `authenticate_header` is what makes DRF answer 401 rather than 403 here.
    assert "Bearer" in response.get("WWW-Authenticate", "")


def test_an_unknown_token_is_401(client, db):
    response = client.get("/probe", **auth("this-token-was-never-issued"))
    assert response.status_code == 401


def test_a_revoked_token_stops_working(client, token):
    instance, plaintext = token
    assert client.get("/probe", **auth(plaintext)).status_code == 200

    instance.revoke()
    assert client.get("/probe", **auth(plaintext)).status_code == 401


def test_a_deactivated_user_cannot_authenticate(client, token, user):
    instance, plaintext = token
    user.is_active = False
    user.save(update_fields=["is_active"])

    assert client.get("/probe", **auth(plaintext)).status_code == 401


def test_an_unknown_and_a_revoked_token_are_indistinguishable(client, token):
    """Distinguishing them would tell an attacker which of their guesses had once been real."""
    instance, plaintext = token
    instance.revoke()

    revoked = client.get("/probe", **auth(plaintext))
    unknown = client.get("/probe", **auth("never-issued-at-all"))

    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json() == unknown.json()


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",  # no token
        "Bearer one two",  # spaces
        "Bearer  ",  # whitespace only
    ],
)
def test_malformed_authorization_headers_are_refused(client, db, header):
    response = client.get("/probe", HTTP_AUTHORIZATION=header)
    assert response.status_code == 401


def test_another_scheme_is_passed_on_rather_than_rejected(client, db):
    """Returning None instead of raising is what lets the authentication classes compose.

    Basic auth is not configured, so the request ends up unauthenticated -- but the bearer class
    must not claim a header that is not its own.
    """
    response = client.get("/probe", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz")
    assert response.status_code == 401


def test_the_plaintext_is_never_stored_so_lookup_is_by_digest(client, token):
    instance, plaintext = token
    assert instance.token_hash == ApiToken.hash_token(plaintext)
    assert plaintext not in instance.token_hash

    # Presenting the digest instead of the token must not work.
    assert client.get("/probe", **auth(instance.token_hash)).status_code == 401


def test_session_auth_still_works_alongside_bearer(client, user):
    """Both mechanisms are configured; enabling one must not disable the other."""
    user.set_password("a-decent-test-password-42")
    user.save(update_fields=["password"])

    assert client.login(email=user.email, password="a-decent-test-password-42")
    assert client.get("/probe").status_code == 200


def test_a_token_carries_only_its_owners_identity(client, db):
    """A token is not a privilege escalation: it authenticates as its user and nothing more."""
    plain_user = User.objects.create_user(email="plain@example.org", display_name="Plain")
    _, plaintext = ApiToken.issue(user=plain_user, name="plain token")

    response = client.get("/probe", **auth(plaintext))
    assert response.json()["email"] == "plain@example.org"
    assert plain_user.is_platform_admin is False
