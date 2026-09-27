"""The contract between `.dogfood.toml` and the portal's URLconf.

`acceptance/run.py` builds each URL as `base_url.rstrip("/") + routes[key]` and fetches it with
`urllib`, which **follows redirects and rewrites a POST into a GET** on a 301 or 302. So a route
that answered only via Django's `APPEND_SLASH` would be measured as a GET to a different path, and
the closed-event check would be testing the wrong request entirely.

Every path advertised in `.dogfood.toml` therefore has to be registered at exactly the string
advertised. These tests read the real file, not a copy, so editing one without the other fails
here rather than in front of a judge.

What is deliberately *not* asserted: that the T2 routes work. They 404 until T2 exists, and
`.dogfood.toml` claims only `["T1"]`. A stub that turned those FAILs into PASSes is exactly what
the honesty rules forbid.
"""

import json
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import Client
from django.urls import Resolver404, resolve
from django.utils import timezone

from accounts.models import ApiToken
from events.models import Event
from projects import api as projects_api
from public import views as public_views

CONFIG_PATH = Path(__file__).resolve().parent.parent / ".dogfood.toml"

T1_ROUTES = ("gallery", "submit")
T2_ROUTES = ("judge_scores", "peer_scores", "csv_export")


@pytest.fixture(scope="module")
def config():
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


def test_the_config_is_valid_toml_and_claims_only_t1(config):
    """`run.py` uses real `tomllib` on Python 3.11+, so a malformed file means zero checks run."""
    assert config["tiers"]["claimed"] == ["T1"]
    assert config["portal"]["base_url"].startswith("http://")


def test_every_route_the_checker_looks_up_is_present(config):
    for key in T1_ROUTES + T2_ROUTES:
        assert config["routes"].get(key), f"routes.{key} is missing"


def test_no_auth_header_contains_a_hash(config):
    """`run.py`'s fallback TOML parser cuts each line at the first `#`, so a token containing one
    would silently become a different token."""
    for role, header in config["auth"].items():
        assert "#" not in header, f"auth.{role} contains a # and would be truncated"


def test_every_auth_header_is_a_bearer_token(config):
    """The checker attaches exactly one header, so it can never also send a CSRF token. Bearer
    auth is what makes the closed-event POST a real test of the deadline."""
    for role, header in config["auth"].items():
        name, _, value = header.partition(":")
        assert name.strip().lower() == "authorization", f"auth.{role} is not an Authorization header"
        assert value.strip().startswith("Bearer "), f"auth.{role} is not a bearer token"


def test_the_submit_route_resolves_with_no_redirect(config):
    path = config["routes"]["submit"]
    assert not path.endswith("/") and "?" not in path
    assert resolve(path).func is projects_api.project_create


def test_the_gallery_route_resolves_with_no_redirect(config):
    path = config["routes"]["gallery"]
    assert not path.endswith("/") and "?" not in path
    assert resolve(path).func is public_views.gallery


@pytest.mark.django_db
def test_the_gallery_answers_200_to_an_anonymous_get(config):
    response = Client().get(config["routes"]["gallery"])
    assert response.status_code == 200


@pytest.mark.parametrize("key", T2_ROUTES)
def test_the_t2_routes_are_honestly_unimplemented(config, key):
    """They must 404, not answer something that looks like success. When T2 lands, this test is
    the reminder to also move `claimed` in `.dogfood.toml`."""
    with pytest.raises(Resolver404):
        resolve(config["routes"][key].split("?")[0])


@pytest.mark.django_db
def test_a_closed_event_refuses_the_checkers_post_to_the_advertised_path(config, make_user):
    """Check 3, in the checker's own request shape (JSON body, one bearer header, no CSRF),
    against an event with the slug the file advertises, closed."""
    submit = config["routes"]["submit"]
    slug = submit.split("/api/events/")[1].split("/")[0]
    now = timezone.now()
    Event.objects.create(
        slug=slug, name="Closed", is_published=True,
        starts_at=now - timedelta(days=6), submissions_open_at=now - timedelta(days=6),
        submissions_close_at=now - timedelta(days=3), judging_ends_at=now + timedelta(days=4),
    )
    _, raw = ApiToken.issue(make_user(), "checker-shaped")
    response = Client().post(
        submit,
        data=json.dumps({"title": "dogfood-late-submission-probe", "summary": "probe"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    # The checker passes on any 4xx. The portal gives a 409 that says why.
    assert response.status_code == 409
    assert response.json()["error"] == "submissions_closed"
