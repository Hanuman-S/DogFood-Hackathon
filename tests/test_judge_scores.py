"""The judge side: the review service, the judge pages and /api/judge/scores.

Projects are created while submissions are still open (the deadline trigger refuses them after
the close); the judging window is then exercised by moving the clock `core.judging` reads.
"""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import ApiToken
from accounts.roles import Role
from core.judging import JudgingNotOpen
from core.models import AuditAction, AuditLog
from events.models import EventMembership
from projects.models import Project, Status
from scoring import services
from scoring.models import Assignment, AssignmentSource, AssignmentStatus, Criterion, Score

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(make_event, make_user, make_team):
    """An event with the standard rubric, two submitted projects, and a judge assigned both."""
    event = make_event()
    services.create_standard_rubric(event)
    now = timezone.now()
    projects = [
        Project.objects.create(team=make_team(event), event=event, name=f"Project {i}",
                               status=Status.SUBMITTED, submitted_at=now)
        for i in range(2)
    ]
    judge = make_user(role=Role.JUDGE, email="judge.one@example.org")
    membership = EventMembership.objects.create(user=judge, event=event, role=Role.JUDGE)
    for i, project in enumerate(projects):
        Assignment.objects.create(judge=membership, project=project, source=AssignmentSource.MANUAL, position=i)
    return {"event": event, "projects": projects, "judge": judge, "membership": membership}


@pytest.fixture
def clock(monkeypatch):
    def at(when):
        monkeypatch.setattr("core.judging.db_now", lambda: when)
    return at


def during(event):
    return event.judging_starts_at + timedelta(hours=1)


FULL = {"functionality": 4, "quality": 3, "innovation": 5}


def api(user):
    _, raw = ApiToken.issue(user, "test")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def post(client, body):
    return client.post("/api/judge/scores", json.dumps(body), content_type="application/json")


# --- the service --------------------------------------------------------------------------------------

def test_draft_then_submit(world, clock):
    event, membership, project = world["event"], world["membership"], world["projects"][0]
    clock(during(event))
    draft = services.save_review(None, membership, project, {"functionality": 4}, "first look")
    assert draft.submitted_at is None and draft.items.count() == 1
    with pytest.raises(services.ReviewError, match="score every criterion"):
        services.save_review(None, membership, project, {"functionality": 4}, submit=True)
    submitted = services.save_review(None, membership, project, FULL, "done", submit=True)
    assert submitted.pk == draft.pk and submitted.submitted_at is not None
    assert {i.criterion.key: int(i.value) for i in submitted.items.select_related("criterion")} == FULL
    actions = list(AuditLog.objects.filter(subject=event.slug).values_list("action", flat=True))
    assert AuditAction.SCORE_SAVED in actions and AuditAction.SCORE_SUBMITTED in actions


def test_saving_a_draft_over_a_submitted_review_reopens_it(world, clock):
    event, membership, project = world["event"], world["membership"], world["projects"][0]
    clock(during(event))
    services.save_review(None, membership, project, FULL, submit=True)
    reopened = services.save_review(None, membership, project, {"functionality": 2})
    assert reopened.submitted_at is None
    assert reopened.items.count() == 1          # a draft keeps exactly what was sent
    entry = AuditLog.objects.filter(action=AuditAction.SCORE_REOPENED).get()
    assert entry.detail["previous"] == {"functionality": "4.00", "quality": "3.00", "innovation": "5.00"}


@pytest.mark.parametrize("values, message", [
    ({"functionality": 6}, "outside"),
    ({"functionality": 0}, "outside"),
    ({"functionality": "four"}, "not a number"),
    ({"functionality": True}, "not a number"),
    ({"functionality": "3.333"}, "two decimal places"),
    ({"speed": 3}, "not criteria"),
])
def test_values_are_validated(world, clock, values, message):
    clock(during(world["event"]))
    with pytest.raises(services.ReviewError, match=message):
        services.save_review(None, world["membership"], world["projects"][0], values)
    assert not Score.objects.exists()


def test_the_window_is_checked_first(world, clock, make_user):
    event, project = world["event"], world["projects"][0]
    outsider = EventMembership.objects.create(user=make_user(), event=event, role=Role.JUDGE)  # not assigned
    for when, code in ((event.judging_starts_at - timedelta(seconds=1), "judging_not_started"),
                       (event.judging_ends_at, "judging_closed")):
        clock(when)
        for membership in (world["membership"], outsider):
            with pytest.raises(JudgingNotOpen) as caught:     # late, not "not assigned"
                services.save_review(None, membership, project, FULL, submit=True)
            assert caught.value.code == code
    assert AuditLog.objects.filter(action=AuditAction.JUDGING_WRITE_REFUSED).count() == 4


def test_only_assigned_projects_can_be_reviewed(world, clock, make_team):
    event, membership = world["event"], world["membership"]
    other = Project.objects.create(team=make_team(event), event=event, name="Not mine",
                                   status=Status.SUBMITTED, submitted_at=timezone.now())
    withdrawn = world["projects"][1]
    Assignment.objects.filter(judge=membership, project=withdrawn).update(status=AssignmentStatus.WITHDRAWN)
    clock(during(event))
    for project in (other, withdrawn):
        with pytest.raises(services.ReviewForbidden):
            services.save_review(None, membership, project, FULL)


def test_progress_is_read_only_and_counts_states(world, clock, make_event, make_user):
    event, membership = world["event"], world["membership"]
    clock(during(event))
    services.save_review(None, membership, world["projects"][0], FULL, submit=True)
    progress = services.judge_progress(membership)
    assert (progress["total_projects"], progress["submitted"], progress["drafts"]) == (2, 1, 0)
    assert [r["status"] for r in progress["projects"]] == ["submitted", "not started"]
    bare = make_event()                                        # no rubric
    bare_membership = EventMembership.objects.create(user=make_user(), event=bare, role=Role.JUDGE)
    services.judge_progress(bare_membership)
    assert not Criterion.objects.filter(event=bare).exists()   # a page view never creates a rubric


def test_only_submitted_reviews_reach_the_engine(world, clock):
    event, membership = world["event"], world["membership"]
    clock(during(event))
    services.save_review(None, membership, world["projects"][0], FULL, submit=True)
    services.save_review(None, membership, world["projects"][1], {"quality": 2})
    inp = services.build_input(event)
    assert [(r.project_id, dict(r.items)) for r in inp.reviews] == [
        (str(world["projects"][0].pk), {"functionality": 4.0, "quality": 3.0, "innovation": 5.0})]
    assert any(e.reason == "draft, not submitted" for e in inp.excluded)


# --- the API -------------------------------------------------------------------------------------------

def test_api_reads_only_the_callers_reviews(world, clock, make_user):
    event, membership = world["event"], world["membership"]
    clock(during(event))
    services.save_review(None, membership, world["projects"][0], FULL, submit=True)
    peer = make_user(role=Role.JUDGE, email="judge.two@example.org")
    peer_membership = EventMembership.objects.create(user=peer, event=event, role=Role.JUDGE)
    Assignment.objects.create(judge=peer_membership, project=world["projects"][0], source=AssignmentSource.MANUAL)
    services.save_review(None, peer_membership, world["projects"][0], {"quality": 1})

    mine = api(world["judge"]).get("/api/judge/scores")
    assert mine.status_code == 200
    assert [s["criteria"] for s in mine.json()["scores"]] == [{"functionality": "4.00", "quality": "3.00", "innovation": "5.00"}]
    assert mine.json()["scores"][0]["submitted"] is True
    assert api(world["judge"]).get("/api/judge/scores?judge=JUDGE.ONE@example.org").status_code == 200
    assert api(world["judge"]).get(f"/api/judge/scores?judge={world['judge'].pk}").status_code == 200


@pytest.mark.parametrize("target", ["judge.two@example.org", "judge_a", "judge", "judge.one", "", "0"])
def test_api_refuses_any_other_judge(world, make_user, target):
    make_user(role=Role.JUDGE, email="judge.two@example.org")
    response = api(world["judge"]).get(f"/api/judge/scores?judge={target}")
    assert response.status_code == 403
    assert AuditLog.objects.filter(action=AuditAction.ACCESS_DENIED, subject__startswith="peer_scores:").exists()


def test_api_refuses_participants_and_anonymous(world, make_user, make_team):
    participant = make_team(world["event"]).captain
    assert api(participant).get("/api/judge/scores").status_code == 403
    assert Client().get("/api/judge/scores").status_code == 401


def test_api_write_order_and_codes(world, clock, make_user, make_team):
    event, judge, project = world["event"], world["judge"], world["projects"][0]
    body = {"event": event.slug, "project_id": project.pk, "scores": FULL, "submit": True}
    clock(event.judging_ends_at)
    closed = post(api(judge), body)
    assert (closed.status_code, closed.json()["error"]) == (409, "judging_closed")
    clock(during(event))
    assert post(api(judge), {**body, "event": "nope"}).status_code == 404
    assert post(api(judge), {**body, "scores": {"functionality": 9}}).json()["error"] == "invalid_review"
    other = Project.objects.create(team=make_team(event), event=event, name="x", status=Status.SUBMITTED,
                                   submitted_at=timezone.now())
    assert post(api(judge), {**body, "project_id": other.pk}).status_code == 403
    ok = post(api(judge), body)
    assert ok.status_code == 200 and ok.json()["submitted"] is True


def test_api_decline(world, clock):
    event, judge, project = world["event"], world["judge"], world["projects"][1]
    clock(during(event))
    response = post(api(judge), {"event": event.slug, "project_id": project.pk, "decline": "my cousin's team"})
    assert response.status_code == 200
    assert Assignment.objects.get(judge=world["membership"], project=project).status == AssignmentStatus.DECLINED
    again = post(api(judge), {"event": event.slug, "project_id": project.pk, "scores": FULL})
    assert again.status_code == 403


# --- the pages -------------------------------------------------------------------------------------------

def test_judge_console_and_review_page(world, clock, client_for, make_team):
    event, judge, project = world["event"], world["judge"], world["projects"][0]
    clock(during(event))
    client = client_for(judge)
    console = client.get(f"/judge/events/{event.slug}/")
    assert console.status_code == 200 and b"Project 0" in console.content
    page = client.get(f"/judge/events/{event.slug}/projects/{project.pk}/")
    assert page.status_code == 200 and b"decline this project" in page.content

    response = client.post(f"/judge/events/{event.slug}/projects/{project.pk}/",
                           {"action": "submit", **{f"criterion_{k}": v for k, v in FULL.items()}, "comment": "nice"})
    assert response.status_code == 302
    assert Score.objects.get(judge=world["membership"], project=project).submitted_at is not None

    other = Project.objects.create(team=make_team(event), event=event, name="x", status=Status.SUBMITTED,
                                   submitted_at=timezone.now())
    assert client.get(f"/judge/events/{event.slug}/projects/{other.pk}/").status_code == 404


def test_the_console_refreshes_its_queue_from_a_partial(world, clock, client_for, make_event, make_user):
    event, judge = world["event"], world["judge"]
    clock(during(event))
    client = client_for(judge)
    console = client.get(f"/judge/events/{event.slug}/")
    assert b"data-refresh-url" in console.content and b"?partial=1" in console.content
    partial = client.get(f"/judge/events/{event.slug}/?partial=1")
    assert partial.status_code == 200
    assert b"<html" not in partial.content and b"Project 0" in partial.content
    # The same gates as the page: an event you do not judge is a 404, a participant is refused.
    assert client.get(f"/judge/events/{make_event().slug}/?partial=1").status_code == 404
    assert client_for(make_user()).get(f"/judge/events/{event.slug}/?partial=1").status_code == 403


def test_a_refused_review_comes_back_as_typed(world, clock, client_for):
    """Submitting with a criterion missing is refused; the page shows what the judge entered,
    not the last saved copy (here: nothing), so nothing typed is lost."""
    event, judge, project = world["event"], world["judge"], world["projects"][0]
    clock(during(event))
    response = client_for(judge).post(
        f"/judge/events/{event.slug}/projects/{project.pk}/",
        {"action": "submit", "criterion_functionality": "4", "comment": "half written"},
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert not Score.objects.filter(judge=world["membership"], project=project).exists()
    assert 'name="criterion_functionality" value="4" checked' in html
    assert "half written" in html


def test_pages_404_for_an_event_you_do_not_judge(world, make_event, make_user, client_for):
    other = make_event()
    client = client_for(world["judge"])
    assert client.get(f"/judge/events/{other.slug}/").status_code == 404


# --- ?judge= resolution ------------------------------------------------------------------------------------

@pytest.fixture
def judged(world, clock):
    """world, with a submitted review by judge.one and a fixture judge id recorded for them."""
    from imports.models import FixtureRef

    clock(during(world["event"]))
    services.save_review(None, world["membership"], world["projects"][0], FULL, "mine", submit=True)
    FixtureRef.objects.create(kind=FixtureRef.Kind.JUDGE, external_id="jdg_99", object_id=world["membership"].pk)
    return world


@pytest.mark.parametrize("name", ["judge.one@example.org", "JUDGE.ONE@EXAMPLE.ORG", "{pk}", "jdg_99", " jdg_99 "])
def test_a_judge_naming_themselves_is_answered(judged, name):
    response = api(judged["judge"]).get("/api/judge/scores", {"judge": name.format(pk=judged["judge"].pk)})
    assert response.status_code == 200
    assert response.json()["judge"] == "judge.one@example.org" and response.json()["count"] == 1


@pytest.mark.parametrize("name", ["judge.two@example.org", "{other_pk}", "jdg_98", "judge_a", "judge.one",
                                  "Judge One", "", "*", "jdg_%"])
def test_a_judge_naming_anyone_else_is_refused_and_audited(judged, make_user, name):
    from imports.models import FixtureRef

    other = make_user(role=Role.JUDGE, email="judge.two@example.org")
    other_membership = EventMembership.objects.create(user=other, event=judged["event"], role=Role.JUDGE)
    FixtureRef.objects.create(kind=FixtureRef.Kind.JUDGE, external_id="jdg_98", object_id=other_membership.pk)
    before = AuditLog.objects.filter(action=AuditAction.ACCESS_DENIED).count()
    response = api(judged["judge"]).get("/api/judge/scores", {"judge": name.format(other_pk=other.pk)})
    assert response.status_code == 403
    assert AuditLog.objects.filter(action=AuditAction.ACCESS_DENIED).count() == before + 1


@pytest.mark.parametrize("name", ["judge.one@example.org", "{pk}", "jdg_99"])
def test_an_organizer_of_the_event_may_name_any_of_its_judges(judged, name):
    organizer = judged["event"].organizer
    response = api(organizer).get("/api/judge/scores", {"judge": name.format(pk=judged["judge"].pk)})
    assert response.status_code == 200
    body = response.json()
    assert (body["judge"], body["read_as"], body["count"]) == ("judge.one@example.org", "organizer", 1)
    assert body["scores"][0]["event"] == judged["event"].slug


def test_an_organizer_sees_only_their_own_events_reviews_of_that_judge(judged, make_event, make_team, clock):
    other_event = make_event()
    project = Project.objects.create(team=make_team(other_event), event=other_event, name="Elsewhere",
                                     status=Status.SUBMITTED, submitted_at=timezone.now())
    elsewhere = EventMembership.objects.create(user=judged["judge"], event=other_event, role=Role.JUDGE)
    Assignment.objects.create(judge=elsewhere, project=project, source=AssignmentSource.MANUAL)
    services.create_standard_rubric(other_event)
    clock(during(other_event))
    services.save_review(None, elsewhere, project, FULL, submit=True)
    body = api(judged["event"].organizer).get("/api/judge/scores", {"judge": str(judged["judge"].pk)}).json()
    assert {s["event"] for s in body["scores"]} == {judged["event"].slug}


def test_an_organizer_of_another_event_is_refused(judged, make_event):
    stranger = make_event().organizer
    for name in ("judge.one@example.org", str(judged["judge"].pk), "jdg_99"):
        assert api(stranger).get("/api/judge/scores", {"judge": name}).status_code == 403


# --- IDOR: one judge's reviews are never reachable by another -----------------------------------------------

@pytest.fixture
def two_judges(world, clock, make_user, make_team):
    """judge A (world's judge) and judge B. B alone is assigned `b_only`; both share project 0."""
    event = world["event"]
    b_user = make_user(role=Role.JUDGE, email="judge.b2@example.org")
    b = EventMembership.objects.create(user=b_user, event=event, role=Role.JUDGE)
    b_only = Project.objects.create(team=make_team(event), event=event, name="B only",
                                    status=Status.SUBMITTED, submitted_at=timezone.now())
    shared = world["projects"][0]
    for project in (b_only, shared):
        Assignment.objects.create(judge=b, project=project, source=AssignmentSource.MANUAL)
    clock(during(event))
    b_review = services.save_review(None, b, b_only, FULL, "B secret notes", submit=True)
    b_shared = services.save_review(None, b, shared, {"functionality": 1, "quality": 1, "innovation": 1},
                                    "B on the shared project", submit=True)
    return {**world, "b": b, "b_user": b_user, "b_only": b_only, "shared": shared,
            "b_review": b_review, "b_shared": b_shared}


def snapshot_of(score):
    score.refresh_from_db()
    return (score.comment, score.submitted_at,
            sorted((i.criterion.key, str(i.value)) for i in score.items.select_related("criterion")))


def test_api_get_never_returns_another_judges_review_whatever_the_parameters(two_judges):
    client = api(two_judges["judge"])
    for params in ({}, {"id": two_judges["b_review"].pk}, {"score": two_judges["b_review"].pk},
                   {"project_id": two_judges["b_only"].pk}, {"event": two_judges["event"].slug}):
        response = client.get("/api/judge/scores", params)
        assert response.status_code == 200, params
        ids = {s["id"] for s in response.json()["scores"]}
        assert two_judges["b_review"].pk not in ids and two_judges["b_shared"].pk not in ids, params
        assert b"B secret notes" not in response.content


@pytest.mark.parametrize("method", ["patch", "put", "delete"])
def test_api_has_no_edit_by_id_route(two_judges, method):
    client = api(two_judges["judge"])
    body = json.dumps({"id": two_judges["b_review"].pk, "comment": "hijacked"})
    response = getattr(client, method)("/api/judge/scores", body, content_type="application/json")
    assert response.status_code == 405
    assert snapshot_of(two_judges["b_review"])[0] == "B secret notes"


def test_api_post_cannot_write_another_judges_review(two_judges):
    before = snapshot_of(two_judges["b_review"])
    client = api(two_judges["judge"])
    event = two_judges["event"].slug
    # B's project, not assigned to A: refused, whatever id is attached
    for extra in ({}, {"id": two_judges["b_review"].pk}, {"score_id": two_judges["b_review"].pk}):
        response = post(client, {"event": event, "project_id": two_judges["b_only"].pk,
                                 "scores": {"functionality": 1}, "comment": "hijacked", "submit": False, **extra})
        assert response.status_code == 403
    assert snapshot_of(two_judges["b_review"]) == before
    # the shared project: A writes A's own review; B's review of it is untouched
    b_shared_before = snapshot_of(two_judges["b_shared"])
    response = post(client, {"event": event, "project_id": two_judges["shared"].pk, "id": two_judges["b_shared"].pk,
                             "scores": FULL, "comment": "A own", "submit": True})
    assert response.status_code == 200 and response.json()["score_id"] != two_judges["b_shared"].pk
    assert snapshot_of(two_judges["b_shared"]) == b_shared_before
    assert Score.objects.get(judge=two_judges["membership"], project=two_judges["shared"]).comment == "A own"


def test_portal_cannot_open_or_post_another_judges_project(two_judges, client_for):
    client = client_for(two_judges["judge"])
    url = f"/judge/events/{two_judges['event'].slug}/projects/{two_judges['b_only'].pk}/"
    before = snapshot_of(two_judges["b_review"])
    assert client.get(url).status_code == 404
    response = client.post(url, {"action": "submit", "criterion_functionality": "1", "criterion_quality": "1",
                                 "criterion_innovation": "1", "comment": "hijacked"})
    assert response.status_code == 404
    assert snapshot_of(two_judges["b_review"]) == before


def test_portal_shows_only_your_own_review_of_a_shared_project(two_judges, client_for):
    client = client_for(two_judges["judge"])
    page = client.get(f"/judge/events/{two_judges['event'].slug}/projects/{two_judges['shared'].pk}/")
    assert page.status_code == 200
    assert b"B on the shared project" not in page.content
    console = client.get(f"/judge/events/{two_judges['event'].slug}/")
    assert b"B only" not in console.content
