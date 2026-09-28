"""C3: the embeddable gallery. Only it may be framed; it sets no cookie; it shows what an anonymous
visitor sees on /projects for the event, and nothing about votes, results, ranks or comments."""

import re
from datetime import timedelta

import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from core.audit import Origin
from core.middleware import EmbedMiddleware
from events.models import Track
from projects import comments
from projects.models import Project, Status, Tag

pytestmark = pytest.mark.django_db


@pytest.fixture
def gallery(make_event, make_team):
    event = make_event()
    web, hardware = (Track.objects.create(event=event, name=n, order=i) for i, n in enumerate(("Web", "Hardware")))
    now = timezone.now()
    projects = []
    for i in range(3):
        p = Project.objects.create(team=make_team(event, name=f"Team {i}"), name=f"Shown {i}", tagline=f"tag {i}",
                                   track=web if i < 2 else hardware, status=Status.SUBMITTED,
                                   submitted_at=now - timedelta(minutes=i))
        projects.append(p)
    projects[0].tags.add(Tag.objects.create(name="python"))
    Project.objects.create(team=make_team(event, name="Draft team"), name="Secret Draft", status=Status.DRAFT)
    event.web, event.hardware, event.projects_list = web, hardware, projects
    return event


def url(event, query=""):
    return f"/embed/events/{event.slug}/gallery" + (f"?{query}" if query else "")


def names(response):
    return re.findall(r'class="embed__name">([^<]+)<', response.content.decode())


# --- framing and cookies ------------------------------------------------------------------------------

def test_the_embed_may_be_framed_by_anyone(gallery):
    response = Client().get(url(gallery))
    assert response.status_code == 200
    csp = response["Content-Security-Policy"]
    assert "frame-ancestors *" in csp and "frame-ancestors 'none'" not in csp
    assert "X-Frame-Options" not in response
    assert "script-src 'self'" in csp and "default-src 'self'" in csp  # everything else stays as strict


@pytest.mark.parametrize("path", ["/", "/projects", "/login", "/events/{slug}", "/projects/{project}",
                                  "/verify", "/events/{slug}/results"])
def test_every_other_page_still_refuses_framing(gallery, path):
    response = Client().get(path.format(slug=gallery.slug, project=gallery.projects_list[0].pk))
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"], path
    assert response["X-Frame-Options"] == "DENY", path


def test_the_organizer_pages_still_refuse_framing(gallery, client_for):
    response = client_for(gallery.organizer).get(f"/organizer/events/{gallery.slug}/")
    assert response.status_code == 200 and response["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]


def test_the_embed_sets_no_cookie_even_to_a_logged_in_browser_with_csrf_and_messages(gallery, client_for):
    client = client_for(gallery.organizer)       # a live session cookie
    client.get("/login")                          # and a CSRF cookie
    assert "csrftoken" in client.cookies and "dogfood_session" in client.cookies
    client.cookies["messages"] = "stale"
    response = client.get(url(gallery))
    assert response.status_code == 200
    assert not response.cookies and "Set-Cookie" not in response
    assert "csrfmiddlewaretoken" not in response.content.decode()
    anonymous = Client().get(url(gallery))
    assert not anonymous.cookies and "Set-Cookie" not in anonymous


def test_the_middleware_clears_any_cookie_on_embed_paths_only():
    def view(request):
        response = HttpResponse("x")
        response.set_cookie("dogfood_session", "abc")
        response.set_cookie("csrftoken", "def")
        return response
    middleware = EmbedMiddleware(view)
    assert not middleware(RequestFactory().get("/embed/events/x/gallery")).cookies
    assert set(middleware(RequestFactory().get("/projects")).cookies) == {"dogfood_session", "csrftoken"}


# --- what it shows -------------------------------------------------------------------------------------

def test_an_unpublished_or_unknown_event_is_404(make_event):
    hidden = make_event(published=False)
    assert Client().get(url(hidden)).status_code == 404
    assert Client().get("/embed/events/no-such-event/gallery").status_code == 404


def test_it_shows_the_submitted_projects_and_never_a_draft(gallery):
    response = Client().get(url(gallery))
    assert sorted(names(response)) == ["Shown 0", "Shown 1", "Shown 2"]
    assert "Secret Draft" not in response.content.decode()


def test_it_shows_no_votes_results_ranks_or_comment_counts(gallery, make_user):
    for i in range(3):
        comments.post_comment(gallery.projects_list[0].pk, f"great {i}", author=make_user(), origin=Origin())
    html = Client().get(url(gallery)).content.decode().lower()
    for word in ("comment", "vote", "ballot", "credit", "influence", "rank", "score", "winner", "place",
                 "result", "#1", "people's choice"):
        assert word not in html, word


def test_links_open_the_portal_in_a_new_tab(gallery):
    with override_settings(PORTAL_BASE_URL="https://portal.example"):
        html = Client().get(url(gallery)).content.decode()
    links = re.findall(r"<a [^>]*>", html)
    assert links and all('target="_blank"' in a and 'rel="noopener"' in a for a in links)
    assert f'href="https://portal.example/projects/{gallery.projects_list[0].pk}"' in html


def test_filters_work_and_bad_values_fall_back_never_500(gallery):
    assert sorted(names(Client().get(url(gallery, f"track={gallery.hardware.pk}")))) == ["Shown 2"]
    assert names(Client().get(url(gallery, "tag=python"))) == ["Shown 0"]
    assert names(Client().get(url(gallery, "sort=name")))[0] == "Shown 0"
    assert names(Client().get(url(gallery, "sort=oldest")))[0] == "Shown 2"
    assert len(names(Client().get(url(gallery, "limit=1")))) == 1
    for bad in ("track=abc", "track=999999", "sort=bogus", "limit=-5", "limit=abc", "limit=", "theme=neon",
                "track=1%00", "limit=1e9"):
        response = Client().get(url(gallery, bad))
        assert response.status_code == 200, bad
        assert len(names(response)) >= 1, bad
    unknown_tag = Client().get(url(gallery, "tag=" + "x" * 500))  # a filter that matches nothing
    assert unknown_tag.status_code == 200 and names(unknown_tag) == [] and "no projects yet" in unknown_tag.content.decode()
    assert 'data-theme="dark"' in Client().get(url(gallery, "theme=neon")).content.decode()
    assert 'data-theme="light"' in Client().get(url(gallery, "theme=light")).content.decode()


def test_the_limit_is_capped_at_24(make_event, make_team):
    event = make_event()
    now = timezone.now()
    for i in range(30):
        Project.objects.create(team=make_team(event, name=f"T{i}"), name=f"P{i}", status=Status.SUBMITTED,
                               submitted_at=now)
    assert len(names(Client().get(url(event, "limit=999")))) == 24
    assert len(names(Client().get(url(event)))) == 12


def test_the_page_loads_only_its_own_script_and_stylesheet(gallery):
    html = Client().get(url(gallery)).content.decode()
    assert re.findall(r'<script src="([^"]+)"', html) == ["/static/js/embed-frame.js"]
    assert re.findall(r'<link rel="stylesheet" href="([^"]+)"', html) == ["/static/css/crt.css"]
    assert "<script>" not in html and " style=" not in html


def test_the_organizer_sees_a_snippet_with_the_portal_url(gallery, client_for):
    with override_settings(PORTAL_BASE_URL="https://portal.example"):
        html = client_for(gallery.organizer).get(f"/organizer/events/{gallery.slug}/").content.decode()
    assert f"https://portal.example/embed/events/{gallery.slug}/gallery" in html
    assert "https://portal.example/static/js/embed.js" in html and "data-dogfood-embed" in html
