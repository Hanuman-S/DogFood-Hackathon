"""The public gallery at /projects, the public project page, and GET /api/projects."""

import pytest
from django.test import Client
from django.utils import timezone

from events.models import Track
from projects.models import Project, Status, Tag

pytestmark = pytest.mark.django_db


@pytest.fixture
def gallery(make_event, make_team):
    event = make_event(slug="spring")
    other = make_event(slug="autumn")
    hidden = make_event(slug="secret", published=False)
    security = Track.objects.create(event=event, name="Security")
    tools = Track.objects.create(event=event, name="Tools")

    def project(ev, name, tagline="", track=None, tags=(), submitted=True, description="", team_name=None):
        p = Project.objects.create(
            team=make_team(ev, name=team_name), name=name, tagline=tagline, track=track,
            description=description,
            status=Status.SUBMITTED if submitted else Status.DRAFT,
            submitted_at=timezone.now() if submitted else None,
        )
        p.tags.set([Tag.objects.get_or_create(name=t)[0] for t in tags])
        return p

    return {
        "event": event, "security": security, "tools": tools,
        "compass": project(event, "Deep Compass", "Navigates cave systems", security, ["rust", "embedded"],
                           description="Uses sonar to map caves.", team_name="Spelunkers"),
        "meadow": project(event, "Small Meadow", "A calm todo list", tools, ["python"]),
        "orbit": project(other, "Copper Orbit", "Satellite tracker", None, ["python", "maps"]),
        "draft": project(event, "Secret Draft", "not finished", submitted=False),
        "unpublished": project(hidden, "Hidden Event Project", "should not show"),
    }


def names(response):
    return {p.name for p in response.context["page"].object_list}


def test_gallery_is_public_and_shows_only_submitted_projects_of_published_events(gallery):
    response = Client().get("/projects")
    assert response.status_code == 200
    assert names(response) == {"Deep Compass", "Small Meadow", "Copper Orbit"}
    assert b"Secret Draft" not in response.content and b"Hidden Event Project" not in response.content


def test_gallery_path_has_no_redirect():
    assert Client().get("/projects").status_code == 200


@pytest.mark.parametrize(
    "q, expected",
    [
        ("compass", {"Deep Compass"}),                     # name, whole word
        ("comp", {"Deep Compass"}),                        # partial word (substring)
        ("todo", {"Small Meadow"}),                        # tagline
        ("sonar", {"Deep Compass"}),                       # description (full-text only)
        ("spelunkers", {"Deep Compass"}),                  # team name
        ("python", {"Small Meadow", "Copper Orbit"}),      # tag
        ("python -satellite", {"Small Meadow"}),           # web-search exclusion
        ('"calm todo"', {"Small Meadow"}),                 # exact phrase
        ("draft", set()),                                  # drafts never match
        ("zzzz", set()),
    ],
)
def test_search(gallery, q, expected):
    assert names(Client().get("/projects", {"q": q})) == expected


def test_filter_by_event_track_and_tag(gallery):
    c = Client()
    assert names(c.get("/projects", {"event": "spring"})) == {"Deep Compass", "Small Meadow"}
    assert names(c.get("/projects", {"event": "spring", "track": gallery["security"].pk})) == {"Deep Compass"}
    assert names(c.get("/projects", {"tag": "python"})) == {"Small Meadow", "Copper Orbit"}
    assert names(c.get("/projects", {"event": "spring", "tag": "python"})) == {"Small Meadow"}


def test_unknown_filters_are_ignored_not_errors(gallery):
    response = Client().get("/projects", {"event": "nope", "track": "abc", "sort": "sideways", "page": "x"})
    assert response.status_code == 200
    assert len(names(response)) == 3


def test_unpublished_event_cannot_be_selected(gallery):
    assert len(names(Client().get("/projects", {"event": "secret"}))) == 3  # ignored, not leaked


def test_sort_by_name(gallery):
    page = Client().get("/projects", {"sort": "name"}).context["page"]
    assert [p.name for p in page.object_list] == ["Copper Orbit", "Deep Compass", "Small Meadow"]


def test_pagination(make_event, make_team):
    event = make_event()
    for i in range(50):
        Project.objects.create(team=make_team(event), name=f"P{i:02d}", status=Status.SUBMITTED, submitted_at=timezone.now())
    first = Client().get("/projects", {"sort": "name"})
    assert len(first.context["page"].object_list) == 48
    second = Client().get("/projects", {"sort": "name", "page": 2})
    assert [p.name for p in second.context["page"].object_list] == ["P48", "P49"]


def test_public_project_page(gallery):
    assert Client().get(f"/projects/{gallery['compass'].pk}").status_code == 200
    assert Client().get(f"/projects/{gallery['draft'].pk}").status_code == 404
    assert Client().get(f"/projects/{gallery['unpublished'].pk}").status_code == 404


def test_api_list_matches_the_page(gallery):
    body = Client().get("/api/projects", {"q": "python"}).json()
    assert body["count"] == 2
    assert {r["name"] for r in body["results"]} == {"Small Meadow", "Copper Orbit"}
    assert "missing_for_submission" not in body["results"][0]


def test_api_detail_is_public_once_submitted(gallery):
    assert Client().get(f"/api/projects/{gallery['compass'].pk}").status_code == 200
    assert Client().get(f"/api/projects/{gallery['draft'].pk}").status_code == 404
    assert Client().patch(f"/api/projects/{gallery['compass'].pk}", "{}", content_type="application/json").status_code == 401


def test_organizer_sees_possible_duplicates(make_event, make_team, client_for):
    event = make_event()
    for _ in range(2):
        Project.objects.create(team=make_team(event), name="Same Name", repo_url="https://x.org/same",
                               status=Status.SUBMITTED, submitted_at=timezone.now())
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/")
    assert len(page.context["duplicates"]) == 2  # same name, and same repository
    assert b"possible duplicates" in page.content
