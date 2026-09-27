"""Regression tests for the organizer fixes of 27 Sep: adding tracks and questions from the event
page, one prize per place, co-organizer invite links, several rubric rows in one save, saving before
submit or preview, the preview's way back, demo tokens that survive a revoke, readable audit rows,
and pre-filled extension forms."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from accounts.models import ApiToken, digest_token
from accounts.roles import Role, roles_in
from core.models import AuditAction, AuditLog
from events import services
from events.models import CustomQuestion, EventMembership, JudgeInvite, Prize, Track
from organizer.export import describe_detail
from projects.models import Project, Status

pytestmark = pytest.mark.django_db


class FakeRequest:
    def __init__(self, user):
        self.user, self.META, self.headers = user, {"REMOTE_ADDR": "10.0.0.1"}, {}


def control(client, event):
    return client.get(f"/organizer/events/{event.slug}/").content.decode()


# --- tracks, questions, prizes -------------------------------------------------------------------


def test_a_track_and_a_question_can_be_added_from_the_event_page(make_event, client_for):
    """The add forms show no "order" field; it used to be required, so every add failed unseen."""
    event = make_event()
    client = client_for(event.organizer)
    Track.objects.create(event=event, name="First", order=4)
    response = client.post(f"/organizer/events/{event.slug}/track/new", {"name": "Hardware", "description": ""})
    assert response.status_code == 302
    assert Track.objects.get(event=event, name="Hardware").order == 5  # goes last
    response = client.post(f"/organizer/events/{event.slug}/question/new",
                           {"prompt": "What did you learn?", "help_text": "", "kind": "short", "choices": ""})
    assert response.status_code == 302 and CustomQuestion.objects.filter(event=event, prompt="What did you learn?").exists()


def test_a_track_keeps_its_place_when_edited_without_an_order(make_event, client_for):
    event = make_event()
    track = Track.objects.create(event=event, name="Security", order=2)
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/track/{track.pk}",
                                     {"name": "Security & privacy", "description": "", "order": ""})
    track.refresh_from_db()
    assert (track.name, track.order) == ("Security & privacy", 2)


def test_one_prize_per_place_per_track(make_event, client_for):
    event = make_event()
    web = Track.objects.create(event=event, name="Web")
    Prize.objects.create(event=event, title="Grand prize", rank=1)
    Prize.objects.create(event=event, title="Best web", rank=1, track=web)
    client = client_for(event.organizer)
    url = f"/organizer/events/{event.slug}/prize/new"
    overall = client.post(url, {"title": "Another grand", "value": "", "rank": "1", "track": "", "description": ""})
    assert overall.status_code == 400 and "already has a #1 prize" in overall.content.decode()
    in_track = client.post(url, {"title": "Web again", "value": "", "rank": "1", "track": web.pk, "description": ""})
    assert in_track.status_code == 400
    assert client.post(url, {"title": "Runner-up", "value": "", "rank": "2", "track": "", "description": ""}).status_code == 302
    assert Prize.objects.filter(event=event).count() == 3
    # editing a prize without changing its place is fine
    prize = Prize.objects.get(title="Grand prize")
    ok = client.post(f"/organizer/events/{event.slug}/prize/{prize.pk}",
                     {"title": "Grand prize!", "value": "$800", "rank": "1", "track": "", "description": ""})
    assert ok.status_code == 302


def test_a_track_in_use_offers_hide_not_delete(make_event, make_team, client_for):
    event = make_event()
    used = Track.objects.create(event=event, name="Used")
    Track.objects.create(event=event, name="Empty")
    Project.objects.create(team=make_team(event), event=event, name="P", track=used)
    page = control(client_for(event.organizer), event)
    assert f"/track/{used.pk}/delete" not in page and "in use" in page
    assert "/delete" in page  # the empty one can still be deleted


# --- co-organizer invites -------------------------------------------------------------------------


def test_an_open_organizer_link_makes_a_new_co_organizer(make_event, client_for):
    event = make_event()
    page = client_for(event.organizer).post(f"/organizer/events/{event.slug}/organizers/invite", {"email": ""})
    assert page.status_code == 200
    link = page.context["link"]
    assert "/invite/organizer/oinv_" in link
    token = link.rsplit("/", 1)[1]
    guest = Client()
    landing = guest.get(f"/invite/organizer/{token}").content.decode()
    assert "co-organizer" in landing and "tracks" not in landing
    response = guest.post(f"/invite/organizer/{token}", {
        "name": "Olu", "email": "olu@example.org", "password1": "correct-horse-9x", "password2": "correct-horse-9x"})
    assert response.status_code == 302 and response["Location"] == f"/organizer/events/{event.slug}/"
    olu = EventMembership.objects.get(event=event, user__email="olu@example.org")
    assert olu.role == Role.ORGANIZER and olu.added_by == event.organizer
    assert AuditLog.objects.filter(action=AuditAction.ORGANIZER_INVITE_ACCEPTED).exists()
    assert AuditLog.objects.filter(action=AuditAction.ORGANIZER_ADDED, detail__via="invite link").exists()
    assert JudgeInvite.objects.get().accepted_by.email == "olu@example.org"


def test_organizer_links_refuse_competitors_and_existing_organizers(make_event, make_team, make_user, client_for):
    event = make_event()
    competitor = make_user()
    make_team(event, members=[competitor])
    with pytest.raises(services.EventRuleError, match="conflict of interest"):
        services.create_organizer_invite(FakeRequest(event.organizer), event, competitor.email)
    invite, raw = services.create_organizer_invite(FakeRequest(event.organizer), event, "")
    with pytest.raises(services.EventRuleError, match="already an organizer"):
        services.accept_judge_invite(FakeRequest(event.organizer), raw, event.organizer)
    with pytest.raises(services.EventRuleError, match="conflict of interest"):
        services.accept_judge_invite(FakeRequest(competitor), raw, competitor)
    assert Role.ORGANIZER not in roles_in(competitor, event)


def test_add_organizer_still_needs_an_email_and_invites_are_listed_and_revocable(make_event, client_for):
    event = make_event()
    client = client_for(event.organizer)
    assert client.post(f"/organizer/events/{event.slug}/organizers", {"email": ""}).status_code == 400
    client.post(f"/organizer/events/{event.slug}/organizers/invite", {"email": "zoe@example.org"})
    invite = JudgeInvite.objects.get(role="organizer")
    assert "zoe@example.org" in control(client, event)
    response = client.post(f"/organizer/events/{event.slug}/judges/invites/{invite.pk}/revoke")
    assert response["Location"].endswith("#organizers")
    invite.refresh_from_db()
    assert invite.revoked_at and AuditLog.objects.filter(action=AuditAction.ORGANIZER_INVITE_REVOKED).exists()


def test_a_judge_and_an_organizer_invite_for_the_same_email_do_not_cancel_each_other(make_event):
    event = make_event()
    request = FakeRequest(event.organizer)
    services.create_judge_invite(request, event, "sam@example.org")
    services.create_organizer_invite(request, event, "sam@example.org")
    assert JudgeInvite.objects.filter(revoked_at__isnull=True).count() == 2


# --- rubric: several rows in one save --------------------------------------------------------------


def test_several_new_criteria_and_a_removal_save_together(make_event, client_for):
    from scoring.models import Criterion

    event = make_event()
    old = Criterion.objects.create(event=event, key="old", label="Old", weight=1, order=1)
    data = {"rubric-TOTAL_FORMS": "4", "rubric-INITIAL_FORMS": "1", "rubric-MIN_NUM_FORMS": "0",
            "rubric-MAX_NUM_FORMS": "20", "action": "save",
            "rubric-0-id": old.pk, "rubric-0-label": "Old", "rubric-0-key": "old", "rubric-0-weight": "1",
            "rubric-0-DELETE": "on",
            "rubric-1-label": "Impact", "rubric-1-weight": "2",
            "rubric-2-label": "Polish", "rubric-2-weight": "1",
            "rubric-3-label": "Scratch", "rubric-3-weight": "1", "rubric-3-DELETE": "on"}  # added, then removed
    response = client_for(event.organizer).post(f"/organizer/events/{event.slug}/rubric", data)
    assert response.status_code == 302
    assert sorted(event.criteria.values_list("label", "weight")) == [("Impact", Decimal("2")), ("Polish", Decimal("1"))]


def test_the_rubric_page_offers_add_criterion_and_a_row_template(make_event, client_for):
    event = make_event()
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/rubric").content.decode()
    assert "data-rubric-add" in page and 'id="rubric-new-row"' in page and "rubric-__prefix__-label" in page
    assert "{#" not in page  # a template comment printed into the page once swallowed the rows


# --- the project editor ----------------------------------------------------------------------------


@pytest.fixture
def draft(make_event, make_team):
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, event=event, name="Quiet Hours")
    return event, team, project


def full(**extra):
    return {"name": "Quiet Hours", "tagline": "Hush", "description": "It is quiet.",
            "repo_url": "https://example.org/q", "demo_video_url": "", "live_url": "", "tags": "", **extra}


def test_save_and_submit_keeps_what_was_typed(draft, client_for):
    event, team, project = draft
    response = client_for(team.captain).post(f"/participant/projects/{project.pk}/", full(then="submit"))
    assert response.status_code == 302
    project.refresh_from_db()
    assert project.tagline == "Hush" and project.status == Status.SUBMITTED


def test_save_and_submit_with_something_missing_saves_and_says_why(draft, client_for):
    event, team, project = draft
    client = client_for(team.captain)
    page = client.post(f"/participant/projects/{project.pk}/", full(then="submit", repo_url=""), follow=True)
    project.refresh_from_db()
    assert project.tagline == "Hush" and project.status == Status.DRAFT
    assert "not submitted" in page.content.decode()


def test_save_and_preview_saves_then_shows_the_preview_with_a_way_back(draft, client_for):
    event, team, project = draft
    client = client_for(team.captain)
    response = client.post(f"/participant/projects/{project.pk}/", full(tagline="Seen in preview", then="preview"))
    assert response["Location"] == f"/participant/projects/{project.pk}/preview"
    page = client.get(response["Location"]).content.decode()
    assert "Seen in preview" in page and 'class="preview-bar"' in page and "back to editing" in page
    edit = client.get(f"/participant/projects/{project.pk}/").content.decode()
    assert 'form="project-form"' in edit and "data-guard-unsaved" in edit


# --- demo tokens ---------------------------------------------------------------------------------


def test_a_revoked_demo_token_is_restored_on_the_next_boot():
    tokens = {"admin": "t-admin", "organizer": "t-org", "judge_a": "t-ja", "judge_b": "t-jb", "participant": "t-p"}
    with override_settings(DEMO_MODE=True, DEMO_TOKENS=tokens):
        call_command("seed_demo", stdout=StringIO())
        ApiToken.objects.filter(digest=digest_token("t-jb")).update(revoked_at=timezone.now())
        call_command("seed_demo", stdout=StringIO())
    assert ApiToken.objects.get(digest=digest_token("t-jb")).revoked_at is None


# --- audit, readable -------------------------------------------------------------------------------


def test_audit_rows_read_without_a_database_client(make_event, client_for):
    assert describe_detail({"old": "2026-01-01T00:00:00+00:00", "new": "2026-01-02T00:00:00+00:00",
                            "reason": "wifi", "event": "x"}) == "2026-01-01T00:00:00Z -> 2026-01-02T00:00:00Z"
    assert describe_detail({"project": 5}, {5: "Lamp Post"}) == "project: Lamp Post"
    event = make_event()
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/track/new", {"name": "Hardware", "description": ""})
    entry = AuditLog.objects.get(action=AuditAction.EVENT_PART_CHANGED)
    assert entry.detail["name"] == "Hardware" and entry.detail["change"] == "created"


def test_the_extension_forms_come_prefilled_with_a_time(make_event, client_for):
    event = make_event()
    page = control(client_for(event.organizer), event)
    end = event.judging_ends_at + timedelta(days=1)
    assert f'value="{end:%H:%M}"' in page and f'value="{end:%Y-%m-%d}"' in page
