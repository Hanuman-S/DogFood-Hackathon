"""`POST /api/events/<slug>/projects` -- the route `.dogfood.toml` advertises as `submit`.

Acceptance check 3 posts to this endpoint as a participant, against the fixture event, which closed
in the past, and passes on any 4xx. Before phase 5 it passed because the route did not exist: an
accidental 404. These tests exist to make sure it passes for the right reason -- a real
`409 submissions_closed` from the deadline guard -- and keeps doing so.

The checker's body is `{"title": ..., "summary": ...}`. That is the vocabulary of
`acceptance/fixtures.json`, so the serializer accepts it as a documented alias; see
`api/serializers.py`. Either way it must not matter: the deadline is decided before the body is
examined, and `test_a_closed_event_refuses_a_body_the_portal_does_not_understand` pins that.
"""

from __future__ import annotations

import datetime as dt

import pytest

from accounts.models import ApiToken
from core import clock
from core.models import AuditAction, AuditLog
from projects.models import Project, ProjectStatus
from tests.conftest import bearer
from tests.factories import (
    add_team_member,
    make_event,
    make_organizer,
    make_team,
    make_track,
    make_user,
)

SUBMIT_PATH = "/api/events/{slug}/projects"

# The exact body the acceptance checker sends.
CHECKER_BODY = {"title": "dogfood-late-submission-probe", "summary": "probe"}


def token_for(user) -> str:
    _instance, plaintext = ApiToken.issue(user=user, name="test")
    return plaintext


@pytest.fixture
def closed_event(db):
    return make_event(name="Closed API Event", open_window=False)


@pytest.fixture
def open_event(db):
    return make_event(name="Open API Event")


# --------------------------------------------------------------------------------------
# the acceptance check, exactly
# --------------------------------------------------------------------------------------


def test_a_closed_event_refuses_the_checkers_request_with_409(api_client, closed_event):
    team = make_team(closed_event)
    participant = team.captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=closed_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 409
    assert response.json()["error"] == "submissions_closed"
    assert response.json()["closed_at"] == clock.iso(closed_event.submissions_close_at)
    # And nothing was created.
    assert not Project.objects.filter(event=closed_event, team=team).exists()


def test_the_refusal_is_in_the_acceptance_checkers_passing_range(api_client, closed_event):
    """`run.py` accepts any `400 <= status < 500`. 409 is inside it -- stated as a test so that a
    future change to the error code cannot quietly move it outside."""
    participant = make_team(closed_event).captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=closed_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(participant)),
    )

    assert 400 <= response.status_code < 500


def test_a_closed_event_refuses_a_body_the_portal_does_not_understand(api_client, closed_event):
    """The ordering promise: deadline before validation.

    If the body were validated first, a POST with no recognisable fields would come back 400 -- a
    4xx that would satisfy the checker while proving nothing about the deadline. It must be 409.
    """
    participant = make_team(closed_event).captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=closed_event.slug),
        {"utter": "nonsense", "fields": [1, 2, 3]},
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 409
    assert response.json()["error"] == "submissions_closed"


def test_the_deadline_is_checked_before_team_membership(api_client, closed_event):
    """A late POST from someone who is not on a team is refused *as late*, not as unauthorized."""
    stranger = make_user()

    response = api_client.post(
        SUBMIT_PATH.format(slug=closed_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(stranger)),
    )

    assert response.status_code == 409


def test_the_refusal_is_recorded_in_the_audit_log(api_client, closed_event):
    participant = make_team(closed_event).captain().user

    api_client.post(
        SUBMIT_PATH.format(slug=closed_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(participant)),
    )

    entry = AuditLog.objects.get(action=AuditAction.REFUSED_DEADLINE)
    assert entry.actor == participant
    assert entry.metadata["attempted"] == "create_project"
    assert entry.metadata["closed_at"] == clock.iso(closed_event.submissions_close_at)


def test_the_route_answers_in_one_hop(api_client, closed_event):
    """No APPEND_SLASH redirect in front of it. `urllib` follows redirects and rewrites a POST into
    a GET on a 301/302, so a redirect here would make the checker measure the wrong request."""
    participant = make_team(closed_event).captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=closed_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code not in (301, 302, 307, 308)


# --------------------------------------------------------------------------------------
# the endpoint is not a refusal stub
# --------------------------------------------------------------------------------------


def test_an_open_event_actually_creates_a_project(api_client, open_event):
    """The other half of the closed-event test. An endpoint that answered 409 unconditionally
    would pass the acceptance check while doing nothing -- which is exactly the kind of stub the
    honesty rules forbid."""
    team = make_team(open_event, name="Real Team")
    participant = team.captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        {"title": "A Genuine Project", "summary": "It really exists.", "tags": ["python", "Python"]},
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "A Genuine Project"
    assert body["status"] == ProjectStatus.DRAFT
    assert body["submitted_at"] is None
    # A created project is a draft, and the response says what it still needs.
    assert "description" in body["missing_to_submit"]

    project = Project.objects.get(pk=body["id"])
    assert project.team == team
    assert project.tagline == "It really exists."
    # Tags normalized and deduplicated.
    assert list(project.project_tags.values_list("tag__name", flat=True)) == ["python"]


def test_the_portals_own_field_names_work_too(api_client, open_event):
    participant = make_team(open_event).captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        {"name": "Native Naming", "tagline": "Using the portal's own words."},
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 201
    assert response.json()["tagline"] == "Using the portal's own words."


def test_a_missing_name_is_a_400(api_client, open_event):
    participant = make_team(open_event).captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        {"summary": "no title at all"},
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 400


def test_a_second_project_for_the_same_team_is_409_project_exists(api_client, open_event):
    participant = make_team(open_event).captain().user
    headers = bearer(token_for(participant))
    path = SUBMIT_PATH.format(slug=open_event.slug)

    assert api_client.post(path, {"title": "First"}, format="json", **headers).status_code == 201
    response = api_client.post(path, {"title": "Second"}, format="json", **headers)

    assert response.status_code == 409
    assert response.json()["error"] == "project_exists"


def test_a_caller_with_no_team_is_refused(api_client, open_event):
    stranger = make_user()

    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(stranger)),
    )

    assert response.status_code == 403
    assert response.json()["error"] == "permission_denied"


def test_an_organizer_cannot_create_a_project_over_the_api(api_client, open_event):
    """Organizers never author. The API must not be a way around the permission matrix."""
    organizer = make_organizer(open_event)

    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(organizer)),
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# authentication and event resolution
# --------------------------------------------------------------------------------------


def test_an_unauthenticated_post_is_refused(api_client, open_event):
    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug), CHECKER_BODY, format="json"
    )

    assert response.status_code in (401, 403)
    assert not Project.objects.filter(event=open_event).exists()


def test_a_bad_token_is_refused(api_client, open_event):
    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer("not-a-real-token"),
    )

    assert response.status_code in (401, 403)


def test_an_unknown_event_is_a_404(api_client, open_event):
    participant = make_team(open_event).captain().user

    response = api_client.post(
        SUBMIT_PATH.format(slug="no-such-event"),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 404


def test_a_bearer_token_needs_no_csrf_token(api_client, open_event):
    """The checker can attach exactly one header, so it can never send a CSRF token as well. This
    is what makes its closed-event POST a real test of the deadline rather than of CSRF."""
    participant = make_team(open_event).captain().user
    api_client.handler.enforce_csrf_checks = True

    response = api_client.post(
        SUBMIT_PATH.format(slug=open_event.slug),
        {"title": "No CSRF Needed"},
        format="json",
        **bearer(token_for(participant)),
    )

    assert response.status_code == 201


# --------------------------------------------------------------------------------------
# against the real fixture event
# --------------------------------------------------------------------------------------


def test_the_real_fixture_event_refuses_a_submission(api_client, fixture_event):
    """The check as it actually runs: the organizers' own event, whose window closed in 2026-03."""
    from teams.models import TeamMember

    membership = TeamMember.objects.filter(event=fixture_event).select_related("user").first()
    assert membership is not None, "the fixture import produced no team members"

    response = api_client.post(
        SUBMIT_PATH.format(slug=fixture_event.slug),
        CHECKER_BODY,
        format="json",
        **bearer(token_for(membership.user)),
    )

    assert response.status_code == 409
    assert response.json()["error"] == "submissions_closed"
