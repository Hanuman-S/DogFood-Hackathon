"""`GET /api/projects` -- the JSON face of the public gallery.

The property worth testing hardest is that it is *the same* gallery: same rows, same filters, same
ordering, same tolerance of garbage. It calls `gallery.selectors.gallery_page`, so these tests are
really asserting that nobody has quietly added a second filter implementation here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from accounts.models import ApiToken
from core import clock
from projects import services
from tests.conftest import bearer
from tests.factories import (
    make_admin,
    make_event,
    make_organizer,
    make_project,
    make_submitted_project,
    make_team,
    make_track,
    make_user,
)

API = "/api/projects"


@pytest.fixture
def event(db):
    return make_event(name="API Gallery Event")


@pytest.fixture
def populated(event):
    team = make_team(event, name="Api Team")
    track = make_track(event, name="Api Track")
    shown = make_submitted_project(team, name="Api Shown", track=track)
    services.set_tags(actor=team.captain().user, project=shown, tags="python, api")

    draft = make_project(make_team(event, name="Api Draft Team"), name="Api Draft")
    hidden = make_submitted_project(make_team(event, name="Api Hidden Team"), name="Api Hidden")
    hidden.hidden_by_organizer = True
    hidden.save()

    return {"event": event, "track": track, "shown": shown, "draft": draft, "hidden": hidden}


def result_ids(response) -> set[int]:
    return {row["id"] for row in response.json()["results"]}


# --------------------------------------------------------------------------------------
# same scoper as the HTML gallery
# --------------------------------------------------------------------------------------


def test_the_api_lists_only_publicly_visible_projects(api_client, populated):
    response = api_client.get(API, {"event": populated["event"].slug})

    assert response.status_code == 200
    assert result_ids(response) == {populated["shown"].pk}


def test_the_api_is_public(api_client, populated):
    """No token, no session: the gallery is public through both doors."""
    assert api_client.get(API).status_code == 200


@pytest.mark.parametrize("role", ["anonymous", "draft_owner", "organizer", "admin"])
def test_the_api_returns_the_same_rows_to_everyone(api_client, populated, role):
    """Viewer-independent, exactly as the HTML gallery is. A token must not widen the result set."""
    event = populated["event"]
    actors = {
        "anonymous": None,
        "draft_owner": populated["draft"].team.captain().user,
        "organizer": make_organizer(event),
        "admin": make_admin(),
    }
    actor = actors[role]

    headers = {}
    if actor is not None:
        _instance, plaintext = ApiToken.issue(user=actor, name=f"{role} token")
        headers = bearer(plaintext)

    response = api_client.get(API, {"event": event.slug}, **headers)

    assert result_ids(response) == {populated["shown"].pk}


def test_the_api_and_the_html_gallery_agree_row_for_row(api_client, client, populated, imported):
    """The strongest form of "same selector": compare the two responses over the whole fixture set
    plus the locally built rows, in order."""
    api = api_client.get(API, {"sort": "name"})
    html = client.get("/projects", {"sort": "name"})

    api_order = [row["id"] for row in api.json()["results"]]
    html_order = [project.pk for project in html.context["projects"]]
    assert api_order == html_order


# --------------------------------------------------------------------------------------
# filters and search, through the API
# --------------------------------------------------------------------------------------


def test_the_api_filters_by_track(api_client, populated):
    response = api_client.get(API, {"track": populated["track"].pk})
    assert result_ids(response) == {populated["shown"].pk}


def test_the_api_filters_by_tag(api_client, populated):
    assert result_ids(api_client.get(API, {"tag": "python"})) == {populated["shown"].pk}
    # Normalized on the way in, like the HTML gallery.
    assert result_ids(api_client.get(API, {"tag": "PYTHON"})) == {populated["shown"].pk}


def test_the_api_searches(api_client, populated):
    assert populated["shown"].pk in result_ids(api_client.get(API, {"q": "Api Shown"}))
    # Partial words too.
    assert populated["shown"].pk in result_ids(api_client.get(API, {"q": "how"}))


def test_the_api_echoes_how_it_understood_the_query(api_client, populated):
    """A `sort=purple` that silently became `newest` is otherwise invisible to a client."""
    body = api_client.get(API, {"sort": "purple", "tag": "PyThOn", "track": "x"}).json()

    assert body["query"]["sort"] == "newest"
    assert body["query"]["tag"] == "python"
    assert body["query"]["track"] is None


# --------------------------------------------------------------------------------------
# pagination metadata
# --------------------------------------------------------------------------------------


def test_the_response_carries_pagination_metadata(api_client, event, settings):
    settings.GALLERY_PAGE_SIZE = 5
    for i in range(12):
        make_submitted_project(make_team(event, name=f"Page Team {i}"), name=f"Paged {i}")

    body = api_client.get(API, {"event": event.slug}).json()

    assert body["count"] == 12
    assert body["page"] == 1
    assert body["pages"] == 3
    assert body["page_size"] == 5
    assert len(body["results"]) == 5
    assert body["previous"] is None
    assert "page=2" in body["next"]


def test_the_next_link_preserves_every_other_filter(api_client, event, settings):
    settings.GALLERY_PAGE_SIZE = 2
    for i in range(5):
        team = make_team(event, name=f"Keep Team {i}")
        project = make_submitted_project(team, name=f"Keep {i}")
        services.set_tags(actor=team.captain().user, project=project, tags="keep")

    body = api_client.get(API, {"tag": "keep", "sort": "name", "event": event.slug}).json()

    for fragment in ("tag=keep", "sort=name", f"event={event.slug}", "page=2"):
        assert fragment in body["next"], f"{fragment} was dropped from the next link"


def test_walking_the_pages_visits_every_project_once(api_client, event, settings):
    settings.GALLERY_PAGE_SIZE = 3
    moment = clock.now() - dt.timedelta(days=1)
    expected = {
        make_submitted_project(
            make_team(event, name=f"Walk Team {i}"), name="Same Name", submitted_at=moment
        ).pk
        for i in range(10)
    }

    seen: list[int] = []
    url = f"{API}?event={event.slug}"
    while url:
        body = api_client.get(url).json()
        seen.extend(row["id"] for row in body["results"])
        url = body["next"]

    assert len(seen) == len(set(seen))
    assert set(seen) == expected


def test_a_page_past_the_end_returns_the_last_page(api_client, populated):
    body = api_client.get(API, {"page": 9999}).json()
    assert body["page"] == body["pages"]


# --------------------------------------------------------------------------------------
# the row shape
# --------------------------------------------------------------------------------------


def test_a_row_carries_what_a_gallery_card_shows(api_client, populated):
    row = next(
        row
        for row in api_client.get(API, {"event": populated["event"].slug}).json()["results"]
        if row["id"] == populated["shown"].pk
    )

    assert row["name"] == "Api Shown"
    assert row["event"] == populated["event"].slug
    assert row["team"] == "Api Team"
    assert row["track"] == "Api Track"
    assert sorted(row["tags"]) == ["api", "python"]
    # UTC, spelled with a Z like everything else the portal emits.
    assert row["submitted_at"].endswith("Z")
    assert row["url"].endswith(f"/projects/{populated['shown'].pk}")
    # No thumbnail was uploaded, so the field is null rather than a broken URL.
    assert row["thumbnail"] is None


def test_a_row_exposes_no_scores_or_ranking(api_client, populated):
    """T1 has no scores. An API that shipped an empty `scores: []` would be claiming a feature."""
    row = api_client.get(API).json()["results"][0]

    for forbidden in ("score", "scores", "rank", "average", "total_score"):
        assert forbidden not in row


def test_the_thumbnail_url_points_at_the_protected_view(api_client, populated, db):
    """Not at a media path: `MEDIA_URL` is None and uploaded bytes are only served by a view that
    re-applies the project's visibility rules."""
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    project = populated["shown"]
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buffer, format="PNG")
    services.set_thumbnail(
        actor=project.team.captain().user,
        project=project,
        upload=SimpleUploadedFile("t.png", buffer.getvalue(), "image/png"),
    )

    row = next(
        row for row in api_client.get(API).json()["results"] if row["id"] == project.pk
    )
    assert row["thumbnail"].endswith(f"/projects/{project.pk}/thumbnail.img")
    assert "/media/" not in row["thumbnail"]


# --------------------------------------------------------------------------------------
# robustness and query cost
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"page": "abc"},
        {"page": "-3"},
        {"sort": "purple"},
        {"track": "not-an-int"},
        {"tag": "'; DROP TABLE projects_project; --"},
        {"q": "&|!():*"},
        {"q": "a" * 500},
        {"event": "../../etc/passwd"},
    ],
    ids=lambda p: str(p)[:30],
)
def test_garbage_parameters_return_200(api_client, populated, params):
    assert api_client.get(API, params).status_code == 200


def test_a_null_byte_in_the_query_is_survivable(api_client, populated):
    assert api_client.get(API, {"q": "api" + chr(0) + "shown"}).status_code == 200


def test_the_api_query_count_is_fixed(api_client, event, django_assert_num_queries):
    """Four queries regardless of how many rows come back: the paginator's COUNT, the page itself,
    and two for the tag prefetch. The HTML gallery adds three more for the filter form's facets,
    which the API does not build."""
    for i in range(12):
        team = make_team(event, name=f"Api Perf Team {i}")
        project = make_submitted_project(team, name=f"Api Perf {i}", track=make_track(event))
        services.set_tags(actor=team.captain().user, project=project, tags=f"t{i}, shared")

    api_client.get(API, {"event": event.slug})  # warm any per-process caches

    with django_assert_num_queries(4):
        response = api_client.get(API, {"event": event.slug})
    assert len(response.json()["results"]) == 12


def test_the_api_route_has_no_trailing_slash_redirect(api_client, populated):
    """Consistent with every other advertised route: one hop, no APPEND_SLASH."""
    response = api_client.get(API)
    assert response.status_code == 200
    assert not str(response.status_code).startswith("30")
