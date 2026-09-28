"""Comments on gallery projects (T3), through the pages and the JSON API. The rules themselves are
tested at the service layer (test_comments_services.py); these check that every surface applies them
the same way, and what a page actually renders."""

import json
from html.parser import HTMLParser

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import ApiToken
from accounts.roles import ADMIN, Role
from core.audit import Origin
from core.markdown import render_comment
from core.models import AuditAction, AuditLog
from projects import comments
from projects.models import Comment, Project, Status
from voting import integrity

pytestmark = pytest.mark.django_db

HERE = Origin(ip_hash="d" * 64)


@pytest.fixture
def setup(make_event, make_team, make_user):
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, name="Quiet Map", status=Status.SUBMITTED, submitted_at=timezone.now())
    return event, team, project


def api(project):
    return f"/api/projects/{project.pk}/comments"


def post_json(client, url, data, **extra):
    return client.post(url, json.dumps(data), content_type="application/json", **extra)


def ids(response):
    return [c["id"] for c in response.json()["results"]]


def bearer(user):
    _, raw = ApiToken.issue(user, "test")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


# --- anonymous ----------------------------------------------------------------------------------------

def test_anonymous_can_read_on_the_page_and_the_api(setup, make_user):
    _, _, project = setup
    comments.post_comment(project.pk, "Hello there", author=make_user(), origin=HERE)
    page = Client().get(f"/projects/{project.pk}")
    assert page.status_code == 200 and "Hello there" in page.content.decode()
    assert "to comment." in page.content.decode() and 'name="body"' not in page.content.decode()
    response = Client().get(api(project))
    assert response.status_code == 200 and response.json()["count"] == 1
    assert response.json()["results"][0]["author"] == "Test User"  # a display name, never an email
    assert "@" not in json.dumps(response.json())


def test_anonymous_cannot_post_through_the_api_401_even_with_csrf_enforced(setup):
    _, _, project = setup
    client = Client(enforce_csrf_checks=True)  # like a real script with no cookies
    response = post_json(client, api(project), {"body": "drive-by"})
    assert response.status_code == 401 and response.json()["error"] == "login_required"
    assert response["WWW-Authenticate"] == "Bearer"
    assert not Comment.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.COMMENT_ANONYMOUS_REFUSED).count() == 1


def test_anonymous_page_post_goes_to_login_and_writes_nothing(setup):
    _, _, project = setup
    response = Client().post(f"/projects/{project.pk}/comments", {"body": "drive-by"})
    assert response.status_code == 302 and response["Location"].startswith("/login")
    assert not Comment.objects.exists()


# --- posting --------------------------------------------------------------------------------------

def test_a_logged_in_user_posts_through_the_page(setup, login_client):
    _, _, project = setup
    client = login_client()
    page = client.get(f"/projects/{project.pk}")
    assert 'name="body"' in page.content.decode()
    response = client.post(f"/projects/{project.pk}/comments", {"body": "Loved the **map**"})
    assert response.status_code == 302 and response["Location"].endswith("#comments")
    html = client.get(f"/projects/{project.pk}").content.decode()
    assert "<strong>map</strong>" in html


def test_api_post_is_201_and_refusals_carry_status_and_code(setup, login_client):
    _, _, project = setup
    client = login_client()
    response = post_json(client, api(project), {"body": "First!"})
    assert response.status_code == 201 and response.json()["body"] == "First!"
    again = post_json(client, api(project), {"body": "First!"})
    assert (again.status_code, again.json()["error"]) == (409, "duplicate_comment")
    empty = post_json(client, api(project), {"body": "   "})
    assert (empty.status_code, empty.json()["error"]) == (400, "invalid_comment")
    bad = client.post(api(project), "{not json", content_type="application/json")
    assert (bad.status_code, bad.json()["error"]) == (400, "bad_request")


def test_a_session_post_to_the_api_still_needs_csrf_and_a_bearer_one_does_not(setup, make_user, client_for):
    _, _, project = setup
    user = make_user()
    client = client_for(user)
    client.handler.enforce_csrf_checks = True
    assert post_json(client, api(project), {"body": "no token"}).status_code == 403
    assert not Comment.objects.exists()
    response = post_json(Client(enforce_csrf_checks=True), api(project), {"body": "with a token"}, **bearer(user))
    assert response.status_code == 201


def test_comments_turned_off(setup, login_client):
    event, _, project = setup
    comments.set_comments_enabled(event, False, actor=event.organizer)
    client = login_client()
    response = post_json(client, api(project), {"body": "hello"})
    assert (response.status_code, response.json()["error"]) == (409, "comments_disabled")
    html = client.get(f"/projects/{project.pk}").content.decode()
    assert "comments are turned off" in html and 'name="body"' not in html


# --- rendering: XSS, links, images -------------------------------------------------------------------

XSS = ('<script>alert(1)</script> [click](javascript:alert(2)) <img src=x onerror=alert(3)> '
       '<a href="https://evil.example" onclick="alert(4)">x</a> [ok](https://example.org)')


class Tags(HTMLParser):
    """Every real tag and attribute in some HTML (escaped text is text, not tags)."""

    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_xss_in_a_comment_is_sanitised_on_the_page(setup, make_user):
    _, _, project = setup
    comments.post_comment(project.pk, XSS, author=make_user(), origin=HERE)
    html = Client().get(f"/projects/{project.pk}").content.decode()
    section = html[html.index('<ol class="comments">'):html.index("</ol>", html.index('<ol class="comments">'))]
    tags = Tags(section).tags
    assert not [t for t, _ in tags if t in ("script", "img", "iframe", "style")]
    assert not [a for _, attrs in tags for a in attrs if a.startswith("on")]
    hrefs = [attrs.get("href", "") for t, attrs in tags if t == "a"]
    assert hrefs == ["https://example.org"]  # the one real link; javascript: never becomes one
    assert "&lt;script&gt;" in section  # shown as text, not run


def test_comment_links_are_nofollow_ugc_noopener():
    html = str(render_comment("see [the repo](https://example.org/repo)"))
    assert 'href="https://example.org/repo"' in html
    assert 'rel="nofollow ugc noopener"' in html


@pytest.mark.parametrize("body", [
    "![pic](https://evil.example/x.png)",
    '<img src="https://evil.example/x.png">',
    "![pic][ref]\n\n[ref]: https://evil.example/x.png",
])
def test_images_in_a_comment_are_stripped(body):
    tags = Tags(str(render_comment(body))).tags
    assert not [t for t, _ in tags if t == "img"]
    assert not [a for _, attrs in tags for a, v in attrs.items() if a == "src"]


def test_an_external_image_could_not_load_anyway(setup, make_user):
    """The third layer: the page's CSP only lets images come from this origin (or data: URIs)."""
    _, _, project = setup
    comments.post_comment(project.pk, "![pic](https://evil.example/x.png)", author=make_user(), origin=HERE)
    response = Client().get(f"/projects/{project.pk}")
    assert "img-src 'self' data:" in response["Content-Security-Policy"]
    assert 'src="https://evil.example' not in response.content.decode()


# --- who sees hidden and deleted comments --------------------------------------------------------------

@pytest.fixture
def three(setup, make_user):
    event, team, project = setup
    author = make_user(email="author@example.org")
    shown = comments.post_comment(project.pk, "shown", author=author, origin=HERE)
    hidden = comments.post_comment(project.pk, "hidden", author=author, origin=HERE)
    deleted = comments.post_comment(project.pk, "deleted", author=author, origin=HERE)
    comments.hide_comment(hidden.pk, "rude", actor=event.organizer)
    comments.delete_own_comment(deleted.pk, actor=author)
    return event, team, project, shown, hidden, deleted


def test_hidden_and_deleted_are_invisible_to_visitor_participant_and_judge(three, make_user, make_team, client_for):
    event, team, project, shown, hidden, deleted = three
    viewers = {
        "visitor": Client(),
        "participant": client_for(make_team(event).captain),
        "team member": client_for(team.captain),
        "judge": client_for(make_user(role=Role.JUDGE)),
    }
    for who, client in viewers.items():
        assert ids(client.get(api(project))) == [shown.pk], who
        html = client.get(f"/projects/{project.pk}").content.decode()
        assert "shown" in html and ">hidden<" not in html and "rude" not in html and "deleted by its author" not in html, who


def test_organizers_and_admins_see_every_comment_with_its_state(three, client_for, make_user):
    event, _, project, shown, hidden, deleted = three
    for client in (client_for(event.organizer), client_for(make_user(role=ADMIN))):
        response = client.get(api(project))
        assert ids(response) == [deleted.pk, hidden.pk, shown.pk]
        states = {c["id"]: (c["hidden"], c["deleted"]) for c in response.json()["results"]}
        assert states == {shown.pk: (False, False), hidden.pk: (True, False), deleted.pk: (False, True)}
        html = client.get(f"/projects/{project.pk}").content.decode()
        assert "hidden: rude" in html and "deleted by its author" in html


def test_a_non_public_projects_comments_are_404_except_for_its_organizers(make_event, make_team, make_user,
                                                                          client_for):
    for published, status in ((True, Status.DRAFT), (False, Status.SUBMITTED)):
        event = make_event(published=published)
        team = make_team(event)
        project = Project.objects.create(team=team, name="Not public", status=status,
                                         submitted_at=timezone.now() if status == Status.SUBMITTED else None)
        for client in (Client(), client_for(team.captain), client_for(make_user(role=Role.JUDGE))):
            response = client.get(api(project))
            assert (response.status_code, response.json()["error"]) == (404, "no_project")
        for client in (client_for(event.organizer), client_for(make_user(role=ADMIN))):
            response = client.get(api(project))
            assert response.status_code == 200 and response.json()["results"] == []


def test_a_draft_has_no_comment_section_even_for_its_team(make_event, make_team, client_for):
    event = make_event()
    team = make_team(event)
    draft = Project.objects.create(team=team, name="Draft", status=Status.DRAFT)
    html = client_for(team.captain).get(f"/projects/{draft.pk}").content.decode()
    assert 'id="comments"' not in html
    response = post_json(client_for(team.captain), api(draft), {"body": "mine"})
    assert (response.status_code, response.json()["error"]) == (404, "no_project")


# --- delete, hide, restore through the API --------------------------------------------------------------

def test_delete_own_through_the_api_and_not_someone_elses(setup, make_user, client_for):
    _, _, project = setup
    author, other = make_user(), make_user()
    comment = comments.post_comment(project.pk, "mine", author=author, origin=HERE)
    response = post_json(client_for(other), f"/api/comments/{comment.pk}/delete", {})
    assert (response.status_code, response.json()["error"]) == (404, "no_comment")
    response = post_json(client_for(author), f"/api/comments/{comment.pk}/delete", {})
    assert response.status_code == 200 and response.json()["deleted"] is True
    assert post_json(Client(), f"/api/comments/{comment.pk}/delete", {}).status_code == 401


def test_hide_and_restore_through_the_api(setup, make_user, make_event, client_for):
    event, _, project = setup
    comment = comments.post_comment(project.pk, "hmm", author=make_user(), origin=HERE)
    url = f"/api/comments/{comment.pk}"
    outsider = client_for(make_event().organizer)
    response = post_json(outsider, f"{url}/hide", {"reason": "mine"})
    assert (response.status_code, response.json()["error"]) == (404, "no_comment")
    assert post_json(client_for(make_user()), f"{url}/hide", {"reason": "x"}).status_code == 403  # portal gate
    organizer = client_for(event.organizer)
    no_reason = post_json(organizer, f"{url}/hide", {})
    assert (no_reason.status_code, no_reason.json()["error"]) == (400, "invalid_moderation")
    response = post_json(organizer, f"{url}/hide", {"reason": "spam"})
    assert response.status_code == 200 and response.json()["hidden"] is True
    assert ids(Client().get(api(project))) == []
    response = post_json(organizer, f"{url}/restore", {"reason": "not spam"})
    assert response.status_code == 200 and response.json()["hidden"] is False
    assert ids(Client().get(api(project))) == [comment.pk]


# --- the organizer's moderation page ---------------------------------------------------------------

def test_moderation_page_gates_hide_restore_and_toggle(three, make_event, make_user, client_for):
    event, _, project, shown, hidden, deleted = three
    url = f"/organizer/events/{event.slug}/comments"
    assert client_for(make_event().organizer).get(url).status_code == 404
    assert client_for(make_user()).get(url).status_code == 403
    organizer = client_for(event.organizer)
    html = organizer.get(url).content.decode()
    assert all(f">{c.pk}<" in html for c in (shown, hidden, deleted))
    organizer.post(f"{url}/{shown.pk}/hide", {"reason": "off-topic"})
    organizer.post(f"{url}/{hidden.pk}/restore", {"reason": "fine after all"})
    shown.refresh_from_db()
    hidden.refresh_from_db()
    assert shown.is_hidden and not hidden.is_hidden
    organizer.post(f"{url}/toggle", {"enabled": "0"})
    event.refresh_from_db()
    assert not event.comments_enabled
    assert client_for(make_user(role=ADMIN)).get(url).status_code == 200


def test_comment_actions_are_on_the_voting_integrity_trail(three, client_for):
    event = three[0]
    actions = {row.code for row in integrity.trail(event)}
    assert {AuditAction.COMMENT_POSTED, AuditAction.COMMENT_HIDDEN, AuditAction.COMMENT_DELETED} <= actions
    html = client_for(event.organizer).get(f"/organizer/events/{event.slug}/voting/integrity").content.decode()
    assert "Hid a comment" in html and "rude" in html


# --- query counts and pages ------------------------------------------------------------------------

# Pinned for an anonymous API read: the project, the gallery check, the page count, the page.
API_READ_QUERIES = 4


@pytest.mark.parametrize("n", [3, 20])
def test_the_api_read_is_a_fixed_number_of_queries(setup, make_user, django_assert_num_queries, n):
    _, _, project = setup
    Comment.objects.bulk_create(Comment(project=project, author=make_user(email=f"a{i}@example.org"), body=f"b{i}")
                                for i in range(n))
    client = Client()
    with django_assert_num_queries(API_READ_QUERIES):
        response = client.get(api(project))
    assert response.json()["count"] == n


def test_pages_of_comments_and_bad_page_numbers(setup, make_user):
    _, _, project = setup
    author = make_user()
    Comment.objects.bulk_create(Comment(project=project, author=author, body=f"c{i}") for i in range(25))
    assert len(Client().get(api(project) + "?page=2").json()["results"]) == 5
    for bad in ("abc", "-1", "999"):
        assert Client().get(api(project) + f"?page={bad}").status_code == 200
        assert Client().get(f"/projects/{project.pk}?cpage={bad}").status_code == 200
