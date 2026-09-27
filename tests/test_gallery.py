"""The public gallery: scoping, search, filters, ordering, pagination and robustness.

Two properties here are worth more than the rest put together:

1. **The listing is viewer-independent.** Everyone -- a stranger, a team with an unsubmitted draft,
   an organizer, a platform admin -- sees exactly the same set of projects. Not "roughly the same":
   identical, asserted as a set comparison.
2. **No query string can produce a 500.** Every parameter is parsed tolerantly, because these URLs
   get typed, truncated, and bookmarked from older versions of a portal.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client

from core import clock
from gallery.selectors import SORTS, GalleryQuery, gallery_page, gallery_queryset
from projects import services
from projects.models import Project, ProjectStatus
from tests.factories import (
    PASSWORD,
    make_admin,
    make_event,
    make_organizer,
    make_project,
    make_submitted_project,
    make_team,
    make_track,
    make_user,
)

GALLERY = "/projects"

# Spelled `chr(0)` rather than written literally: a real null byte in a .py file makes the file
# unimportable ("source code string cannot contain null bytes"), and an escape in a string literal
# is easy for an editor or a code generator to mangle.
NULL_BYTE = chr(0)


@pytest.fixture
def event(db):
    return make_event(name="Gallery Event")


@pytest.fixture
def populated(event):
    """One of everything the gallery has to cope with."""
    visible_team = make_team(event, name="Visible Team")
    draft_team = make_team(event, name="Draft Team")
    hidden_team = make_team(event, name="Hidden Team")
    dupe_team = make_team(event, name="Duplicate Team")

    track = make_track(event, name="Accessibility")
    shown = make_submitted_project(visible_team, name="Shown Project", track=track)
    draft = make_project(draft_team, name="Draft Project")
    hidden = make_submitted_project(hidden_team, name="Hidden Project")
    hidden.hidden_by_organizer = True
    hidden.save()
    duplicate = make_submitted_project(dupe_team, name="Duplicate Project")
    duplicate.duplicate_of = shown
    duplicate.save()

    return {
        "event": event,
        "track": track,
        "shown": shown,
        "draft": draft,
        "hidden": hidden,
        "duplicate": duplicate,
        "visible_team": visible_team,
        "draft_team": draft_team,
    }


def ids(queryset_or_list) -> set[int]:
    return {project.pk for project in queryset_or_list}


def query(**kwargs) -> GalleryQuery:
    return GalleryQuery.from_params(kwargs)


# --------------------------------------------------------------------------------------
# 1. the scoper is viewer-independent
# --------------------------------------------------------------------------------------


def test_the_gallery_shows_only_submitted_canonical_unhidden_public_projects(populated):
    shown = ids(gallery_queryset(query()))

    assert populated["shown"].pk in shown
    assert populated["draft"].pk not in shown
    assert populated["hidden"].pk not in shown
    assert populated["duplicate"].pk not in shown


def test_a_project_in_a_private_gallery_event_is_not_listed(db):
    private = make_event(name="Private Gallery", gallery_public=False)
    project = make_submitted_project(make_team(private), name="Invisible")

    assert project.pk not in ids(gallery_queryset(query()))


def test_the_selector_takes_no_user_at_all(populated):
    """The strongest form of "viewer-independent": there is no argument to get wrong.

    A signature that accepted a user would be one refactor away from someone passing
    `visible_projects(user)` through it.
    """
    import inspect

    from gallery import selectors

    for name in ("gallery_queryset", "gallery_page"):
        parameters = inspect.signature(getattr(selectors, name)).parameters
        assert "user" not in parameters
        assert "request" not in parameters


def test_every_viewer_sees_an_identical_gallery(client, populated):
    """A team member with a draft, an organizer, an admin and a stranger get the same set.

    This is the participant trap the design exists to avoid: if a team saw their own draft in the
    public listing they would assume it was public and never press submit.
    """
    event = populated["event"]
    draft_owner = populated["draft_team"].captain().user
    organizer = make_organizer(event)
    admin = make_admin()

    def listing_for(user=None) -> set[int]:
        page_client = Client()
        if user is not None:
            assert page_client.login(email=user.email, password=PASSWORD)
        response = page_client.get(GALLERY, {"event": event.slug})
        assert response.status_code == 200
        return {project.pk for project in response.context["projects"]}

    anonymous = listing_for()
    assert anonymous == listing_for(draft_owner) == listing_for(organizer) == listing_for(admin)
    # And it really is the public set, not "everything" -- otherwise the equality above would be
    # satisfied by showing all four projects to everyone.
    assert anonymous == {populated["shown"].pk}


def test_the_gallery_never_calls_the_viewer_dependent_scoper(monkeypatch, populated):
    """`visible_projects(user)` is the right scoper for a detail page and the wrong one here.

    Asserted by making it explode: any accidental use shows up as a failure in this test rather
    than as a draft appearing in the public listing months later.
    """
    from core import permissions

    def forbidden(*args, **kwargs):
        raise AssertionError("the gallery must not use visible_projects()")

    monkeypatch.setattr(permissions, "visible_projects", forbidden)

    result = gallery_page(query(), with_facets=True)
    assert result.total >= 1


def test_a_draft_is_still_reachable_at_its_own_url_by_its_team(client, populated):
    """The listing is public; the detail page is not. Both statements have to hold at once."""
    draft = populated["draft"]
    owner = populated["draft_team"].captain().user

    assert client.get(f"/projects/{draft.pk}").status_code == 404
    assert client.login(email=owner.email, password=PASSWORD)
    assert client.get(f"/projects/{draft.pk}").status_code == 200


# --------------------------------------------------------------------------------------
# 2. search
# --------------------------------------------------------------------------------------


@pytest.fixture
def searchable(event):
    team = make_team(event, name="Kiln Works")
    project = make_submitted_project(
        team,
        name="Glass Signal",
        tagline="A lighthouse for radio telemetry",
        description="Built with Elixir and a small amount of despair.",
    )
    services.set_tags(actor=team.captain().user, project=project, tags="elixir, telemetry")
    other = make_submitted_project(make_team(event, name="Other Team"), name="Unrelated Thing")
    return project, other


def test_search_matches_the_name(searchable):
    project, other = searchable
    found = ids(gallery_queryset(query(q="Glass Signal")))
    assert project.pk in found
    assert other.pk not in found


@pytest.mark.parametrize("term", ["sig", "GLASS", "glas", "ignal"])
def test_search_matches_a_partial_word_in_the_name(searchable, term):
    """The search vector holds lexemes, so it matches whole words only. A visitor typing three
    letters is the commonest search there is, so the name is also matched as a substring (backed by
    the pg_trgm index from migration 0003)."""
    project, _other = searchable
    assert project.pk in ids(gallery_queryset(query(q=term)))


def test_search_matches_the_tagline_and_description(searchable):
    project, _other = searchable
    assert project.pk in ids(gallery_queryset(query(q="lighthouse")))
    assert project.pk in ids(gallery_queryset(query(q="despair")))


def test_search_matches_a_tag_and_the_team_name(searchable):
    """Tags and the team name carry weight B in the vector."""
    project, _other = searchable
    assert project.pk in ids(gallery_queryset(query(q="telemetry")))
    assert project.pk in ids(gallery_queryset(query(q="Kiln")))


def test_search_stems_english_words(searchable):
    """Stemming is why the vector exists rather than a LIKE over four columns.

    Note the english stemmer handles regular inflection, not irregular verbs: "lighthouses" finds
    "lighthouse", but "build" does **not** find "built" -- both stem to themselves. That gap is one
    of the reasons the name is also matched as a substring.
    """
    project, _other = searchable
    assert project.pk in ids(gallery_queryset(query(q="lighthouses")))
    assert project.pk in ids(gallery_queryset(query(q="telemetries")))


@pytest.mark.parametrize(
    "term",
    [
        "glass & ",            # to_tsquery syntax: a database error if passed through raw
        "glass | signal",
        "!glass",
        "glass:*",
        "(((",
        "'unbalanced",
        '"unclosed phrase',
        "glass <-> signal",
        ":",
        "&|!()",
        "\\",
        "%",
        "a" * 300,
    ],
)
def test_search_never_raises_on_operator_syntax(searchable, term):
    """`to_tsquery` raises a `ProgrammingError` on every one of these, which is a 500 in a search
    box. `websearch_to_tsquery` treats them as text. This test is the reason
    `search_type="websearch"` must never be changed to `"raw"`."""
    assert isinstance(ids(gallery_queryset(query(q=term))), set)


def test_a_quoted_phrase_is_honoured(searchable):
    """One of the things websearch syntax buys: a phrase search that people expect to work."""
    project, _other = searchable
    assert project.pk in ids(gallery_queryset(query(q='"radio telemetry"')))
    assert project.pk not in ids(gallery_queryset(query(q='"telemetry radio"')))


def test_a_negated_word_is_honoured(searchable):
    project, _other = searchable
    assert project.pk not in ids(gallery_queryset(query(q="signal -lighthouse")))


# --------------------------------------------------------------------------------------
# 2b. tags filter exactly, not through the vector
# --------------------------------------------------------------------------------------


def test_the_tag_filter_is_an_exact_normalized_match(event):
    team_a = make_team(event, name="A Team")
    team_b = make_team(event, name="B Team")
    tagged = make_submitted_project(team_a, name="Tagged")
    other = make_submitted_project(team_b, name="Differently Tagged")
    services.set_tags(actor=team_a.captain().user, project=tagged, tags="react")
    services.set_tags(actor=team_b.captain().user, project=other, tags="reactivity")

    found = ids(gallery_queryset(query(tag="react")))

    assert tagged.pk in found
    # "reactivity" stems to "reactiv" and would collide with "react" in a vector match. An exact
    # tag match is what keeps a tag pill meaning exactly that tag.
    assert other.pk not in found


def test_the_tag_filter_normalizes_the_incoming_value(event):
    team = make_team(event, name="Case Team")
    project = make_submitted_project(team, name="Cased")
    services.set_tags(actor=team.captain().user, project=project, tags="machine learning")

    for raw in ["Machine Learning", " MACHINE   LEARNING ", "machine learning"]:
        assert project.pk in ids(gallery_queryset(query(tag=raw))), raw


def test_a_project_with_several_tags_appears_once(event):
    """The tag join multiplies rows; `distinct()` is what stops a project appearing three times."""
    team = make_team(event, name="Multi Tag")
    project = make_submitted_project(team, name="Many Tags")
    services.set_tags(actor=team.captain().user, project=project, tags="a, b, c")

    listed = [p.pk for p in gallery_queryset(query())]
    assert listed.count(project.pk) == 1


# --------------------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------------------


def test_the_event_filter_scopes_to_one_event(db):
    first = make_event(name="First Event")
    second = make_event(name="Second Event")
    here = make_submitted_project(make_team(first), name="Here")
    there = make_submitted_project(make_team(second), name="There")

    found = ids(gallery_queryset(query(event=first.slug)))

    assert here.pk in found
    assert there.pk not in found


def test_the_track_filter_scopes_to_one_track(event):
    wanted = make_track(event, name="Wanted Track")
    other = make_track(event, name="Other Track")
    a = make_submitted_project(make_team(event, name="TA"), name="In Track", track=wanted)
    b = make_submitted_project(make_team(event, name="TB"), name="Out Of Track", track=other)

    found = ids(gallery_queryset(query(track=wanted.pk)))

    assert a.pk in found
    assert b.pk not in found


def test_filters_combine(event):
    track = make_track(event, name="Combined")
    team = make_team(event, name="Combining Team")
    match = make_submitted_project(team, name="Matches Everything", track=track)
    services.set_tags(actor=team.captain().user, project=match, tags="python")
    make_submitted_project(make_team(event, name="Wrong Track Team"), name="Matches Everything Else")

    found = ids(
        gallery_queryset(query(event=event.slug, track=track.pk, tag="python", q="Matches"))
    )

    assert found == {match.pk}


def test_a_filter_naming_something_that_does_not_exist_matches_nothing(populated):
    """Deliberately empty rather than ignored: dropping an unresolvable filter would widen the
    result set and show projects the visitor never asked for."""
    assert ids(gallery_queryset(query(track=999_999))) == set()
    assert ids(gallery_queryset(query(tag="no-such-tag"))) == set()
    assert ids(gallery_queryset(query(event="no-such-event"))) == set()


# --------------------------------------------------------------------------------------
# 3. ordering always has an id tiebreak
# --------------------------------------------------------------------------------------


def test_every_sort_ends_with_a_primary_key_tiebreak():
    """Without a total order, LIMIT/OFFSET can show one project twice across two pages and omit
    another entirely."""
    for key, ordering in SORTS.items():
        assert ordering[-1] in ("id", "-id"), f"sort {key!r} has no id tiebreak: {ordering}"


def test_projects_submitted_in_the_same_instant_have_a_stable_order(event):
    """The tie the tiebreak exists for. Ten projects, one timestamp."""
    moment = clock.now() - dt.timedelta(days=1)
    made = [
        make_submitted_project(
            make_team(event, name=f"Tie Team {i}"), name="Identical Name", submitted_at=moment
        )
        for i in range(10)
    ]
    assert {p.submitted_at for p in made} == {moment}

    first = [p.pk for p in gallery_queryset(query(sort="newest"))]
    second = [p.pk for p in gallery_queryset(query(sort="newest"))]
    assert first == second
    # And descending by id, given the timestamps tie.
    ours = [pk for pk in first if pk in ids(made)]
    assert ours == sorted(ours, reverse=True)


def test_newest_first_orders_by_submission_time(event):
    old = make_submitted_project(
        make_team(event, name="Old"), name="Older", submitted_at=clock.now() - dt.timedelta(days=3)
    )
    new = make_submitted_project(
        make_team(event, name="New"), name="Newer", submitted_at=clock.now() - dt.timedelta(hours=1)
    )

    listed = [p.pk for p in gallery_queryset(query(event=event.slug, sort="newest"))]
    assert listed.index(new.pk) < listed.index(old.pk)


def test_name_sort_is_alphabetical(event):
    zed = make_submitted_project(make_team(event, name="Z Team"), name="Zebra")
    ant = make_submitted_project(make_team(event, name="A Team"), name="Antelope")

    listed = [p.pk for p in gallery_queryset(query(event=event.slug, sort="name"))]
    assert listed.index(ant.pk) < listed.index(zed.pk)


def test_paging_covers_every_project_exactly_once(event, settings):
    """The property a tiebreak buys, tested the way it actually matters: walk every page and check
    the union is the whole set with no repeats."""
    settings.GALLERY_PAGE_SIZE = 3
    moment = clock.now() - dt.timedelta(days=1)
    made = {
        make_submitted_project(
            make_team(event, name=f"Page Team {i}"), name="Same Name", submitted_at=moment
        ).pk
        for i in range(10)
    }

    seen: list[int] = []
    page_number = 1
    while True:
        result = gallery_page(query(event=event.slug, page=page_number))
        seen.extend(p.pk for p in result.projects)
        if not result.page.has_next():
            break
        page_number += 1

    assert len(seen) == len(set(seen)), "a project appeared on two pages"
    assert set(seen) == made


# --------------------------------------------------------------------------------------
# 4. garbage input never 500s
# --------------------------------------------------------------------------------------


GARBAGE = [
    {"page": "abc"},
    {"page": "0"},
    {"page": "-1"},
    {"page": "99999999"},
    {"page": "1e9"},
    {"page": ["1", "2"]},
    {"page": "1; DROP TABLE projects_project"},
    {"sort": "purple"},
    {"sort": ""},
    {"sort": "-submitted_at"},
    {"sort": "id) --"},
    {"track": "abc"},
    {"track": "-5"},
    {"track": "99999999999999999999"},
    {"track": "1 OR 1=1"},
    {"tag": "'; DROP TABLE x; --"},
    {"tag": "a" * 500},
    {"tag": "<script>alert(1)</script>"},
    {"event": "../../etc/passwd"},
    {"event": "a" * 500},
    {"q": "&|!():*"},
    {"q": "\x00null byte"},
    {"q": "a" * 500},
    {"q": "%"},
    {"page": "2", "sort": "nope", "track": "x", "tag": "", "event": "", "q": "'"},
]


@pytest.mark.parametrize("params", GARBAGE, ids=lambda p: str(p)[:40])
def test_garbage_parameters_render_a_page_rather_than_failing(client, populated, params):
    response = client.get(GALLERY, params)

    assert response.status_code == 200, f"{params} produced {response.status_code}"


@pytest.mark.parametrize("params", GARBAGE, ids=lambda p: str(p)[:40])
def test_garbage_parameters_parse_into_a_coherent_query(params):
    """Not just "no exception": the parsed query has to be usable."""
    parsed = GalleryQuery.from_params(params)

    assert parsed.sort in SORTS
    assert parsed.page >= 1
    assert parsed.track is None or parsed.track >= 1
    assert len(parsed.tag) <= 60
    assert len(parsed.q) <= 200


def test_a_null_byte_in_the_search_term_does_not_reach_the_database(client, populated):
    """Postgres rejects a null byte in a query parameter with a hard error, so it has to be gone
    before the query is built."""
    assert client.get(GALLERY, {"q": "glass\x00signal"}).status_code == 200


def test_a_page_past_the_end_returns_the_last_page(populated):
    result = gallery_page(query(page=9999))

    assert result.page.number == result.page.paginator.num_pages


# --------------------------------------------------------------------------------------
# 5. no N+1
# --------------------------------------------------------------------------------------


def test_the_gallery_page_query_count_is_fixed(client, event, django_assert_num_queries):
    """Pinned, not bounded: rendering 12 cards must cost the same as rendering 2.

    Without the `select_related`/`prefetch_related` in `gallery_page`, each card costs a query for
    the team, one for the event and one for its tags -- 36 extra on this page, and 150 on a full
    one. The most-reloaded page in the portal is exactly where that matters.
    """
    for i in range(12):
        team = make_team(event, name=f"Perf Team {i}")
        project = make_submitted_project(team, name=f"Perf Project {i}", track=make_track(event))
        services.set_tags(actor=team.captain().user, project=project, tags=f"tag{i}, shared")

    # Warm up anything cached per process (content types, the session table) so the count reflects
    # the page's own queries.
    client.get(GALLERY, {"event": event.slug})

    # Seven, and constant: one COUNT for the paginator, one for the page of rows, two for the
    # `project_tags__tag` prefetch (the join table, then the tags), and three for the filter form's
    # facets (events, tracks, tags).
    with django_assert_num_queries(7):
        response = client.get(GALLERY, {"event": event.slug})
    assert response.status_code == 200
    assert len(response.context["projects"]) == 12


def test_the_query_count_does_not_grow_with_the_number_of_projects(client, event):
    """The other half: measure at two sizes and compare, so the pinned number above cannot be
    satisfied by a page that happens to be small."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def count_for(n: int) -> int:
        for i in range(n):
            team = make_team(event, name=f"Scale Team {n}-{i}")
            project = make_submitted_project(team, name=f"Scale {n}-{i}")
            services.set_tags(actor=team.captain().user, project=project, tags="scale")
        client.get(GALLERY, {"event": event.slug})
        with CaptureQueriesContext(connection) as captured:
            client.get(GALLERY, {"event": event.slug})
        return len(captured.captured_queries)

    small = count_for(2)
    large = count_for(20)
    assert small == large, f"{small} queries for 2 projects, {large} for 22"


# --------------------------------------------------------------------------------------
# 6. works without JavaScript; htmx is enhancement only
# --------------------------------------------------------------------------------------


def test_the_filter_form_is_a_plain_get_form(client, populated):
    html = client.get(GALLERY).content.decode()

    assert 'method="get"' in html
    # No POST, so no CSRF token is needed and the URL carries the state.
    assert "csrfmiddlewaretoken" not in html
    assert '<button type="submit"' in html


def test_filtering_works_through_the_url_alone(client, populated):
    """What a visitor with JavaScript disabled -- or a curl user -- actually does."""
    response = client.get(GALLERY, {"event": populated["event"].slug, "sort": "name"})

    assert response.status_code == 200
    assert {p.pk for p in response.context["projects"]} == {populated["shown"].pk}


def test_an_htmx_request_returns_only_the_results_region(client, populated):
    full = client.get(GALLERY)
    partial = client.get(GALLERY, HTTP_HX_REQUEST="true")

    assert partial.status_code == 200
    assert b"<form" not in partial.content  # no page furniture
    # Same rows either way: the header changes the wrapper, never the content.
    assert {p.pk for p in full.context["projects"]} == {p.pk for p in partial.context["projects"]}


def test_the_page_references_no_external_asset(client, populated):
    """The offline rule. `tests/test_smoke.py` sweeps the templates; this checks the rendered page."""
    html = client.get(GALLERY).content.decode()

    for host in ("cdn.", "googleapis", "unpkg", "jsdelivr", "//fonts."):
        assert host not in html


# --------------------------------------------------------------------------------------
# 8. still T1: nothing about scores
# --------------------------------------------------------------------------------------


def test_the_gallery_exposes_no_scores_or_ranking(client, populated):
    html = client.get(GALLERY).content.decode().lower()

    for forbidden in ("score", "rank", "winner", "average", "leaderboard"):
        assert forbidden not in html, f"the T1 gallery mentions {forbidden!r}"


def test_the_gallery_ordering_is_never_randomised():
    """A randomised gallery is a judging aid (it reduces position bias), which is T2's business and
    would also make pagination incoherent."""
    for ordering in SORTS.values():
        assert "?" not in ordering


# --------------------------------------------------------------------------------------
# the per-event gallery
# --------------------------------------------------------------------------------------


def test_the_per_event_gallery_scopes_by_its_path(client, db):
    first = make_event(name="Path Event One")
    second = make_event(name="Path Event Two")
    here = make_submitted_project(make_team(first), name="Path Here")
    there = make_submitted_project(make_team(second), name="Path There")

    response = client.get(f"/events/{first.slug}/projects")

    listed = {p.pk for p in response.context["projects"]}
    assert here.pk in listed
    assert there.pk not in listed


def test_the_path_event_wins_over_a_query_parameter(client, db):
    """Otherwise `/events/a/projects?event=b` would show b's projects under a's heading."""
    first = make_event(name="Wins Event One")
    second = make_event(name="Wins Event Two")
    here = make_submitted_project(make_team(first), name="Wins Here")
    there = make_submitted_project(make_team(second), name="Wins There")

    response = client.get(f"/events/{first.slug}/projects", {"event": second.slug})

    listed = {p.pk for p in response.context["projects"]}
    assert here.pk in listed
    assert there.pk not in listed


def test_an_event_with_its_gallery_switched_off_is_a_404_not_an_empty_page(client, db):
    """The difference matters to the organizer who just switched it off and wants to be sure."""
    private = make_event(name="Switched Off", gallery_public=False)

    assert client.get(f"/events/{private.slug}/projects").status_code == 404


def test_an_unknown_event_path_is_a_404(client, db):
    assert client.get("/events/no-such-event/projects").status_code == 404


# --------------------------------------------------------------------------------------
# the acceptance checks this phase is for
# --------------------------------------------------------------------------------------


def test_the_gallery_is_public_and_answers_200(client, populated):
    """Acceptance check 1, exactly: a GET with no auth header."""
    assert client.get(GALLERY).status_code == 200


def test_the_first_three_fixture_titles_appear_on_page_one(client, imported):
    """Acceptance check 2, exactly.

    `run.py` takes the first three projects in file order and passes if **any** title appears as a
    case-insensitive substring of the response body. All three are asserted here, because passing on
    one of three is a coin flip that a later sort change could lose.

    Nothing in the portal special-cases these titles or the checker; they are present because the
    default page size fits the whole fixture cohort on page one.
    """
    body = client.get(GALLERY).content.decode().lower()

    for title in ("glass signal", "small meadow", "deep compass"):
        assert title in body, f"{title!r} is not on page one of the gallery"


def test_the_whole_visible_fixture_cohort_fits_on_page_one(client, imported):
    """Why check 2 passes under any sort: there is no page two for the fixture event.

    41 fixture projects, one of which is flagged as a duplicate, so 40 are visible -- inside the
    default page size of 50. If someone lowers `GALLERY_PAGE_SIZE` below 40, this fails here rather
    than in the acceptance report.
    """
    from django.conf import settings
    from events.models import Event

    fixture_event = Event.objects.get(external_id="evt_01")
    visible = gallery_queryset(query(event=fixture_event.slug)).count()

    assert visible == 40
    assert visible <= settings.GALLERY_PAGE_SIZE

    response = client.get(GALLERY, {"event": fixture_event.slug})
    assert response.context["page"].paginator.num_pages == 1
