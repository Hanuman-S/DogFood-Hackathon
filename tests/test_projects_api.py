"""The JSON API for events and projects."""

import json

import pytest
from django.test import Client

from accounts.models import ApiToken
from accounts.roles import Role
from projects.models import Project, Status

pytestmark = pytest.mark.django_db


def bearer(user):
    _, raw = ApiToken.issue(user, "t")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def post(path, data, auth, method="post"):
    return getattr(Client(), method)(path, json.dumps(data), content_type="application/json", **auth)


def test_events_list_and_detail(make_event):
    event = make_event()
    make_event(published=False)
    listing = Client().get("/api/events").json()["events"]
    assert [e["slug"] for e in listing] == [event.slug]
    detail = Client().get(f"/api/events/{event.slug}").json()
    assert detail["phase"] == "open" and detail["tracks"] == []


def test_participant_starts_a_project_over_the_api(make_event, make_user):
    event = make_event()
    user = make_user()
    response = post(f"/api/events/{event.slug}/projects", {"name": "API thing", "tagline": "Made by curl", "tags": ["Go", "go"]}, bearer(user))
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "API thing" and body["tagline"] == "Made by curl" and body["tags"] == ["go"]
    assert body["status"] == "draft" and "repo_url" in body["missing_for_submission"]
    assert Project.objects.get().team.captain == user


@pytest.mark.parametrize("role", [Role.JUDGE, Role.ORGANIZER])
def test_staff_of_the_event_cannot_start_projects_in_it(make_event, make_user, role):
    from events.models import EventMembership

    event = make_event()
    staff = make_user()
    EventMembership.objects.create(user=staff, event=event, role=role)
    response = post(f"/api/events/{event.slug}/projects", {"name": "No"}, bearer(staff))
    assert response.status_code == 409
    assert not Project.objects.exists()


def test_platform_admins_cannot_start_projects(make_event, make_user):
    from accounts.roles import ADMIN

    event = make_event()
    response = post(f"/api/events/{event.slug}/projects", {"name": "No"}, bearer(make_user(role=ADMIN)))
    assert response.status_code == 409
    assert not Project.objects.exists()


def test_a_judge_elsewhere_may_start_a_project_here(make_event, make_user):
    event = make_event()
    response = post(f"/api/events/{event.slug}/projects", {"name": "Yes"}, bearer(make_user(role=Role.JUDGE)))
    assert response.status_code == 201


def test_patch_is_partial_and_validated(make_event, make_team):
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, name="Keep", tagline="keep me")
    auth = bearer(team.captain)
    response = post(f"/api/projects/{project.pk}", {"repo_url": "https://github.com/a/b"}, auth, method="patch")
    assert response.status_code == 200
    assert response.json()["tagline"] == "keep me" and response.json()["repo_url"] == "https://github.com/a/b"
    bad = post(f"/api/projects/{project.pk}", {"repo_url": "javascript:alert(1)"}, auth, method="patch")
    assert bad.status_code == 400 and "repo_url" in bad.json()["fields"]


def test_submit_over_the_api(make_event, make_team):
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, name="P")
    auth = bearer(team.captain)
    refused = post(f"/api/projects/{project.pk}/submit", {}, auth)
    assert refused.status_code == 409 and "description" in refused.json()["missing"]
    post(f"/api/projects/{project.pk}", {"tagline": "t", "description": "d", "repo_url": "https://x.org/r"}, auth, method="patch")
    done = post(f"/api/projects/{project.pk}/submit", {}, auth)
    assert done.status_code == 200 and done.json()["status"] == "submitted"
    assert Project.objects.get().status == Status.SUBMITTED


def test_non_members_cannot_edit_and_strangers_cannot_see_drafts(make_event, make_team, make_user):
    event = make_event()
    project = Project.objects.create(team=make_team(event), name="Secret draft")
    outsider = bearer(make_user())
    assert Client().get(f"/api/projects/{project.pk}", **outsider).status_code == 404
    assert post(f"/api/projects/{project.pk}", {"name": "x"}, outsider, method="patch").status_code == 404
    organizer = bearer(event.organizer)
    assert Client().get(f"/api/projects/{project.pk}", **organizer).status_code == 200
    assert post(f"/api/projects/{project.pk}", {"name": "x"}, organizer, method="patch").status_code == 403


def test_bad_json_is_a_400(make_event, make_user):
    event = make_event()
    response = Client().post(f"/api/events/{event.slug}/projects", "{nope", content_type="application/json", **bearer(make_user()))
    assert response.status_code == 400
