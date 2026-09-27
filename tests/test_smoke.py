"""Phase 1 smoke tests: the skeleton is wired correctly.

Small, but they catch the failures that waste the most time later -- a health check that
lies, a static asset that is actually a CDN link, an admin door standing open.
"""

import pytest
from django.conf import settings
from django.urls import reverse

from accounts.models import ApiToken, User


# --------------------------------------------------------------------------------------
# health check
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_healthz_reports_ok_with_a_reachable_database(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # UTC, with the `Z` spelling used everywhere else.
    assert body["time"].endswith("Z")


@pytest.mark.django_db
def test_healthz_needs_no_authentication(client):
    """A health probe that needs a credential is a health probe that gets switched off."""
    assert client.get("/healthz").status_code == 200


def test_healthz_is_registered_without_a_trailing_slash():
    """The compose healthcheck and .dogfood.toml both use the exact string `/healthz`.

    `urllib` follows redirects and rewrites POST to GET on the way, so a route that only
    answers via an APPEND_SLASH redirect is a route that behaves differently than advertised.
    """
    assert reverse("healthz") == "/healthz"


# --------------------------------------------------------------------------------------
# offline-safety: no external asset references
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_home_page_renders_and_references_no_external_hosts(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode()

    # The whole adoptability premise is "it comes up with the network off". A CDN link in
    # the layout breaks that silently -- the page still renders, just unstyled, and only on
    # a machine that has internet. So it is asserted rather than trusted.
    for forbidden in (
        "//cdn.",
        "cdnjs",
        "jsdelivr",
        "unpkg",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "http://localhost:3000",
    ):
        assert forbidden not in html, f"external asset reference found: {forbidden}"

    assert "/static/vendor/pico.min.css" in html
    assert "/static/vendor/htmx.min.js" in html


def test_vendored_assets_are_actually_committed():
    """Guards against a `.gitignore` rule or a stray clean quietly removing them."""
    vendor = settings.BASE_DIR / "static" / "vendor"
    for name in ("pico.min.css", "htmx.min.js", "NOTICE.md"):
        asset = vendor / name
        assert asset.exists(), f"{name} is missing from src/static/vendor/"
        assert asset.stat().st_size > 0


@pytest.mark.django_db
def test_every_page_states_that_times_are_utc(client):
    """A hackathon deadline misread by a timezone is the most expensive mistake this
    software can cause, so the layout says UTC on every page."""
    html = client.get("/").content.decode()
    assert "UTC" in html


# --------------------------------------------------------------------------------------
# admin door
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_is_closed_to_anonymous_visitors(client):
    response = client.get("/admin/", follow=True)
    # Django's admin answers with its login form rather than a 403; what matters is that the
    # index content is not served.
    assert b"Platform administration" not in response.content


@pytest.mark.django_db
def test_admin_is_closed_to_a_non_admin_user(client):
    User.objects.create_user(
        email="participant@example.org",
        password="not-a-simple-password-9",
        display_name="Participant",
    )
    assert client.login(email="participant@example.org", password="not-a-simple-password-9")

    response = client.get("/admin/", follow=True)
    assert b"Platform administration" not in response.content


@pytest.mark.django_db
def test_admin_opens_for_a_platform_admin(client):
    User.objects.create_superuser(
        email="admin@example.org",
        password="not-a-simple-password-9",
        display_name="Admin",
    )
    assert client.login(email="admin@example.org", password="not-a-simple-password-9")

    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"Platform administration" in response.content


@pytest.mark.django_db
def test_is_staff_alone_does_not_open_the_admin():
    """The portal's own flag is what grants access, not Django's weaker `is_staff`.

    Keeps a stray `is_staff=True` -- set by a fixture, a script or a mis-click -- from
    becoming platform-admin access.
    """
    from core.admin_site import portal_admin_site

    user = User.objects.create_user(
        email="staffish@example.org",
        password="not-a-simple-password-9",
        display_name="Staffish",
        is_staff=True,
    )

    class FakeRequest:
        pass

    request = FakeRequest()
    request.user = user
    assert portal_admin_site.has_permission(request) is False

    user.is_platform_admin = True
    assert portal_admin_site.has_permission(request) is True


# --------------------------------------------------------------------------------------
# user model
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_email_is_the_login_identifier_and_is_stored_lowercased():
    user = User.objects.create_user(
        email="  MiXeD.Case@Example.ORG ",
        password="not-a-simple-password-9",
        display_name="Mixed Case",
    )
    user.refresh_from_db()
    assert user.email == "mixed.case@example.org"
    assert User.USERNAME_FIELD == "email"


@pytest.mark.django_db
def test_users_without_a_password_cannot_be_logged_into(client):
    """Imported fixture participants start with an unusable password.

    Unusable is not blank: no input matches it, so the account is inert rather than open.
    """
    User.objects.create_user(email="imported@example.org", display_name="Imported")
    assert client.login(email="imported@example.org", password="") is False
    assert client.login(email="imported@example.org", password="dogfood-demo") is False


@pytest.mark.django_db
def test_superuser_sets_the_portal_flag_and_the_django_flags_together():
    """An account that is an admin to Django but not to the portal (or vice versa) is a
    confusing half-state; `create_superuser` sets all three."""
    admin = User.objects.create_superuser(
        email="root@example.org",
        password="not-a-simple-password-9",
        display_name="Root",
    )
    assert admin.is_platform_admin and admin.is_staff and admin.is_superuser
    assert admin.can_create_events


# --------------------------------------------------------------------------------------
# api tokens
# --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_api_token_stores_only_a_hash():
    user = User.objects.create_user(email="dev@example.org", display_name="Dev")
    token, plaintext = ApiToken.issue(user=user, name="laptop")

    assert plaintext
    assert token.token_hash != plaintext
    assert len(token.token_hash) == 64  # sha-256 hex

    # The plaintext must not be recoverable from the row in any field.
    row = ApiToken.objects.filter(pk=token.pk).values().first()
    assert plaintext not in str(row)


@pytest.mark.django_db
def test_api_token_plaintext_contains_no_character_that_breaks_dogfood_toml():
    """Tokens are pasted into `.dogfood.toml`, whose fallback parser truncates a line at the
    first `#`. A token that cannot contain `#`, a quote or whitespace cannot break that file.
    """
    user = User.objects.create_user(email="dev2@example.org", display_name="Dev")
    for _ in range(25):
        _, plaintext = ApiToken.issue(user=user, name="probe")
        assert not any(ch in plaintext for ch in '#"\'\\ \t\n')


@pytest.mark.django_db
def test_generated_tokens_are_unique_and_long_enough():
    user = User.objects.create_user(email="dev3@example.org", display_name="Dev")
    seen = {ApiToken.issue(user=user, name="t")[1] for _ in range(20)}
    assert len(seen) == 20
    assert all(len(value) >= 32 for value in seen)


@pytest.mark.django_db
def test_revoking_a_token_is_recorded_and_idempotent():
    user = User.objects.create_user(email="dev4@example.org", display_name="Dev")
    token, _ = ApiToken.issue(user=user, name="ci")

    assert token.is_active
    token.revoke()
    first = token.revoked_at
    assert first is not None
    assert not token.is_active

    token.revoke()
    assert token.revoked_at == first
