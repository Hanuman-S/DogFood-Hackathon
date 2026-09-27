"""The contract between `.dogfood.toml` and the portal's URLconf.

`acceptance/run.py` builds each URL as `base_url.rstrip("/") + routes[key]` and fetches it with
`urllib`, which **follows redirects and rewrites a POST into a GET** on a 301 or 302. So a route
that answers only via Django's `APPEND_SLASH` would be measured as a GET to a different path, and
the closed-event check would be testing the wrong request entirely.

Every path advertised in `.dogfood.toml` therefore has to be registered at exactly the string
advertised. These tests read the real file -- not a copy -- so editing one without the other fails
here rather than in front of a judge.

What is deliberately *not* asserted: that the T2 routes work. They 404 until T2 exists, the four
T2 acceptance checks are expected to FAIL, and `.dogfood.toml` claims only `["T1"]`. A stub that
turned those FAILs into PASSes is exactly what the honesty rules forbid.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from django.urls import Resolver404, resolve

from accounts.models import ApiToken
from api.views import ProjectCreateView
from gallery.views import project_gallery
from core import clock
from events.models import Event
from tests.conftest import bearer
from tests.factories import make_event, make_team

CONFIG_PATH = Path(__file__).resolve().parent.parent / ".dogfood.toml"

# The keys `run.py` looks up, and which tier each belongs to.
T1_ROUTES = ("gallery", "submit")
T2_ROUTES = ("judge_scores", "peer_scores", "csv_export")


@pytest.fixture(scope="module")
def config() -> dict:
    with CONFIG_PATH.open("rb") as handle:
        return tomllib.load(handle)


# --------------------------------------------------------------------------------------
# the file itself
# --------------------------------------------------------------------------------------


def test_the_config_is_valid_toml(config):
    """`run.py` uses real `tomllib` on Python 3.11+, so a malformed file means zero checks run."""
    assert config["tiers"]["claimed"] == ["T1"]
    assert config["portal"]["base_url"].startswith("http://")


def test_every_route_the_checker_looks_up_is_present(config):
    for key in T1_ROUTES + T2_ROUTES:
        assert config["routes"].get(key), f"routes.{key} is missing"


def test_no_token_contains_a_hash(config):
    """`run.py`'s fallback TOML parser truncates each line at the first `#`, so a token containing
    one would silently become a different token. `ApiToken`'s alphabet excludes it by
    construction; this asserts the file as it actually stands."""
    for role, header in config["auth"].items():
        assert "#" not in header, f"auth.{role} contains a # and would be truncated"


def test_every_auth_header_is_a_bearer_token(config):
    """The checker can attach exactly one header, so it can never also send a CSRF token. Bearer
    auth is what makes the closed-event POST a real test of the deadline."""
    for role, header in config["auth"].items():
        name, _, value = header.partition(":")
        assert name.strip().lower() == "authorization", f"auth.{role} is not an Authorization header"
        assert value.strip().startswith("Bearer "), f"auth.{role} is not a bearer token"


# --------------------------------------------------------------------------------------
# the routes resolve where they are advertised
# --------------------------------------------------------------------------------------


def test_the_submit_route_resolves_with_no_redirect(config):
    """The T1 write path. Registered at exactly the advertised string, so `urllib` reaches it in
    one hop and posts what it meant to post."""
    path = config["routes"]["submit"]
    assert not path.endswith("/")

    # `resolve` raising would mean the path is not registered at all; a redirect would mean it is
    # registered only with a trailing slash. Compare the view class, since DRF's `as_view()`
    # wrapper is named `view`.
    match = resolve(path)
    assert getattr(match.func, "cls", None) is ProjectCreateView


def test_the_gallery_route_resolves_with_no_redirect(config):
    """Check 1's route. It must be registered at the root at exactly this string: under the
    `projects/` include it would answer only via an APPEND_SLASH redirect, which `urllib` follows --
    so the checker would measure a different path."""
    path = config["routes"]["gallery"]
    assert not path.endswith("/")

    match = resolve(path)
    assert match.func is project_gallery


def test_the_gallery_answers_200_to_an_anonymous_get(client, db, config):
    """Check 1, as the checker sends it: a GET with no auth header at all."""
    response = client.get(config["routes"]["gallery"])

    assert response.status_code == 200


def test_the_gallery_route_does_not_redirect_over_http(client, db, config):
    """Belt and braces around the resolver test: assert the real response is not a 3xx, because a
    redirect is what silently turns the checker's request into something else."""
    response = client.get(config["routes"]["gallery"])

    assert response.status_code not in (301, 302, 307, 308)


def test_the_advertised_t1_paths_carry_no_query_string(config):
    """A route string may carry a query string -- `run.py` concatenates it raw -- but neither T1
    route needs one, and a stray `?` would make the path unresolvable."""
    for key in T1_ROUTES:
        assert "?" not in config["routes"][key]


@pytest.mark.parametrize("key", T2_ROUTES)
def test_the_t2_routes_are_honestly_unimplemented(config, key):
    """They must 404, not answer something that looks like success.

    Written as an assertion rather than left to chance so that a future session adding a T2 route
    has to come here and update this test deliberately -- which is the moment to also move
    `claimed` in `.dogfood.toml`.
    """
    path = config["routes"][key].split("?")[0]
    with pytest.raises(Resolver404):
        resolve(path)


# --------------------------------------------------------------------------------------
# check 3, end to end through the real advertised path
# --------------------------------------------------------------------------------------


def test_a_closed_event_refuses_a_post_to_the_advertised_path(api_client, db, config):
    """The acceptance check's own request shape, against the path the file advertises.

    The fixture event is the real target when the checker runs; this uses a purpose-built closed
    event so the assertion holds even if the fixture file changes. `tests/test_api_submit.py`
    covers the fixture event itself.
    """
    submit = config["routes"]["submit"]
    slug_in_config = submit.split("/api/events/")[1].split("/")[0]

    # The event this path points at may already exist -- it is the fixture event, imported once
    # per session -- and the slug is unique, so reuse it when it is there rather than depending on
    # which test file ran first.
    event = Event.objects.filter(slug=slug_in_config).first()
    if event is None:
        event = make_event(name="Advertised Slug Event", open_window=False)
        event.slug = slug_in_config
        event.save(update_fields=["slug"])
    assert event.submissions_close_at < clock.now(), (
        "the event the submit route points at must be closed for this check to mean anything"
    )
    participant = make_team(event).captain().user
    _token, plaintext = ApiToken.issue(user=participant, name="checker-shaped")

    response = api_client.post(
        submit,
        {"title": "dogfood-late-submission-probe", "summary": "probe"},
        format="json",
        **bearer(plaintext),
    )

    # The checker passes on any 4xx. It gets a 409 that says why.
    assert 400 <= response.status_code < 500
    assert response.status_code == 409
    assert response.json()["error"] == "submissions_closed"
