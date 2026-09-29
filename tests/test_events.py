"""Events: creation, who may manage them, visibility, and their tracks, prizes and questions."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from accounts.roles import ADMIN, Role
from core.models import AuditAction, AuditLog
from events.models import CustomQuestion, Event, Phase, Prize, Track
from projects.models import Answer, Project

from _dates import dt_fields

pytestmark = pytest.mark.django_db


DATE_FIELDS = ("starts_at", "submissions_open_at", "submissions_close_at", "judging_starts_at",
               "judging_ends_at", "results_at")


def event_form(**overrides):
    """The event form as a browser posts it. Dates are given as "YYYY-MM-DDTHH:MM" (or "" for
    empty) and posted as the date box and time box the form actually has."""
    values = {
        "name": "Spring Hack 2027",
        "slug": "",
        "tagline": "48 hours",
        "description": "## Hello",
        "starts_at": "2027-03-01T08:00",
        "submissions_open_at": "2027-03-01T09:00",
        "submissions_close_at": "2027-03-03T09:00",
        "judging_starts_at": "2027-03-03T12:00",
        "judging_ends_at": "2027-03-06T09:00",
        "results_at": "",
        "min_team_size": "1",
        "max_team_size": "4",
    }
    values.update(overrides)
    data = {}
    for name, value in values.items():
        data.update(dt_fields(name, value) if name in DATE_FIELDS else {name: value})
    return data


def test_organizer_creates_an_unpublished_event_they_manage(make_user, client_for):
    organizer = make_user(role=Role.ORGANIZER)
    client = client_for(organizer)
    response = client.post("/organizer/events/new", event_form())
    assert response.status_code == 302 and response["Location"] == "/organizer/events/spring-hack-2027/"
    event = Event.objects.get(slug="spring-hack-2027")
    assert not event.is_published
    assert list(event.members_with(Role.ORGANIZER)) == [organizer]
    assert event.submissions_close_at.isoformat() == "2027-03-03T09:00:00+00:00"  # typed as UTC
    assert AuditLog.objects.filter(action=AuditAction.EVENT_CREATED, subject=event.slug).exists()


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"submissions_close_at": "2027-02-28T09:00"}, "submissions_close_at"),
        ({"judging_ends_at": "2027-03-02T09:00"}, "judging_ends_at"),
        ({"starts_at": "2027-03-04T09:00"}, "submissions_open_at"),
        # every date strictly after the one before: equal is refused too
        ({"starts_at": "2027-03-01T09:00"}, "submissions_open_at"),
        ({"judging_starts_at": "2027-03-03T09:00"}, "judging_starts_at"),
        ({"judging_starts_at": "2027-03-06T09:00"}, "judging_ends_at"),
        ({"results_at": "2027-03-06T09:00"}, "results_at"),
        ({"results_at": "2027-03-05T09:00"}, "results_at"),
        # required fields
        ({"judging_starts_at": ""}, "judging_starts_at"),
        ({"tagline": ""}, "tagline"),
        ({"description": "  "}, "description"),
        ({"max_team_size": "0"}, "max_team_size"),
        ({"max_team_size": "21"}, "max_team_size"),
        ({"min_team_size": "0"}, "min_team_size"),
        ({"min_team_size": "5"}, "min_team_size"),  # larger than the max of 4
    ],
)
def test_nonsense_dates_sizes_and_missing_fields_are_refused(make_user, client_for, overrides, field):
    client = client_for(make_user(role=Role.ORGANIZER))
    response = client.post("/organizer/events/new", event_form(**overrides))
    assert response.status_code == 400
    assert field in response.context["form"].errors
    assert not Event.objects.exists()


def test_a_date_without_a_time_is_refused_not_guessed(make_user, client_for):
    """The browser's calendar fills the date box only; the time must be picked too."""
    client = client_for(make_user(role=Role.ORGANIZER))
    data = event_form()
    data["submissions_close_at_1"] = ""
    response = client.post("/organizer/events/new", data)
    assert response.status_code == 400
    assert "Pick a time as well as the date." in response.context["form"].errors["submissions_close_at"]


def test_results_are_optional_and_every_date_is_stored_as_typed_in_utc(make_user, client_for):
    client = client_for(make_user(role=Role.ORGANIZER))
    assert client.post("/organizer/events/new", event_form(results_at="2027-03-08T18:00")).status_code == 302
    event = Event.objects.get()
    assert event.judging_starts_at.isoformat() == "2027-03-03T12:00:00+00:00"
    assert event.results_at.isoformat() == "2027-03-08T18:00:00+00:00"
    event.delete()
    assert client.post("/organizer/events/new", event_form()).status_code == 302
    assert Event.objects.get().results_at is None


def test_the_new_event_form_starts_with_every_date_empty_and_autofill_off(make_user, client_for):
    page = client_for(make_user(role=Role.ORGANIZER)).get("/organizer/events/new").content.decode()
    for name in DATE_FIELDS:
        for part, kind in (("0", "date"), ("1", "time")):
            tag = page.split(f'name="{name}_{part}"')[0].rsplit("<input", 1)[1] + page.split(f'name="{name}_{part}"')[1].split(">", 1)[0]
            assert f'type="{kind}"' in tag and 'autocomplete="off"' in tag and "value=" not in tag


def test_the_database_refuses_an_inverted_window(make_event):
    event = make_event()
    event.submissions_close_at = event.submissions_open_at - timedelta(hours=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        event.save()


def test_slug_must_be_unique(make_event, make_user, client_for):
    make_event(slug="taken")
    client = client_for(make_user(role=Role.ORGANIZER))
    assert client.post("/organizer/events/new", event_form(slug="taken")).status_code == 400


@pytest.mark.parametrize("typed, slug", [
    ("example.org", "example-org"),
    ("Spring Hack 2027", "spring-hack-2027"),
    ("my_event/2027", "my-event-2027"),
    ("--Edge--", "edge"),
])
def test_url_name_is_normalized_not_refused(make_user, client_for, typed, slug):
    # "example.org" used to be refused with Django's "Enter a valid slug" before it was cleaned.
    client = client_for(make_user(role=Role.ORGANIZER))
    response = client.post("/organizer/events/new", event_form(slug=typed))
    assert response.status_code == 302 and response["Location"] == f"/organizer/events/{slug}/"


def test_url_name_of_only_punctuation_is_refused_in_words(make_user, client_for):
    client = client_for(make_user(role=Role.ORGANIZER))
    response = client.post("/organizer/events/new", event_form(slug="...", name="!!!"))
    assert response.status_code == 400
    assert "Give the event a url name." in response.content.decode()


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_organizers_and_admins_create_events(make_user, client_for, role):
    client = client_for(make_user(role=role))
    assert client.post("/organizer/events/new", event_form()).status_code == 403
    assert not Event.objects.filter(slug="spring-hack-2027").exists()


def test_other_organizers_cannot_see_or_edit_an_event(make_event, make_user, client_for):
    event = make_event(published=False)
    stranger = client_for(make_user(role=Role.ORGANIZER))
    assert stranger.get(f"/organizer/events/{event.slug}/").status_code == 404
    assert stranger.post(f"/organizer/events/{event.slug}/publish", {"publish": "1"}).status_code == 404
    event.refresh_from_db()
    assert not event.is_published


def test_admins_manage_every_event(make_event, make_user, client_for):
    event = make_event()
    admin = client_for(make_user(role=ADMIN))
    assert admin.get(f"/organizer/events/{event.slug}/").status_code == 200


def test_unpublished_events_are_invisible_to_the_public(make_event, make_user, client_for):
    event = make_event(published=False)
    assert Client().get(f"/events/{event.slug}").status_code == 404
    assert Client().get("/api/events").json()["events"] == []
    assert client_for(event.organizer).get(f"/events/{event.slug}").status_code == 200  # preview


def test_publishing_makes_it_public_and_is_audited(make_event, client_for):
    event = ready_to_publish(make_event(published=False))
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/publish", {"publish": "1"})
    assert Client().get(f"/events/{event.slug}").status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.EVENT_PUBLISHED).exists()


def ready_to_publish(event):
    from scoring.services import create_standard_rubric

    Event.objects.filter(pk=event.pk).update(tagline="48 hours", description="## Hello")
    create_standard_rubric(event)
    event.refresh_from_db()
    return event


@pytest.mark.parametrize("missing, sentence", [
    ("tagline", "add a tagline"),
    ("description", "add a description"),
    ("rubric", "set up the rubric"),
])
def test_an_incomplete_event_cannot_be_published(make_event, client_for, missing, sentence):
    event = ready_to_publish(make_event(published=False))
    if missing in ("tagline", "description"):
        Event.objects.filter(pk=event.pk).update(**{missing: ""})
    elif missing == "rubric":
        event.criteria.all().delete()
    else:
        event.criteria.filter(key="quality").update(weight=10)
    client = client_for(event.organizer)
    page = client.get(f"/organizer/events/{event.slug}/").content.decode()
    assert sentence in page  # the checklist says what is missing before anyone clicks
    response = client.post(f"/organizer/events/{event.slug}/publish", {"publish": "1"}, follow=True)
    assert sentence in response.content.decode()
    event.refresh_from_db()
    assert not event.is_published
    assert not AuditLog.objects.filter(action=AuditAction.EVENT_PUBLISHED).exists()


def test_phase_is_computed_from_the_dates(make_event):
    now = timezone.now()
    event = make_event(
        submissions_open_at=now + timedelta(days=1), submissions_close_at=now + timedelta(days=2),
        starts_at=now, judging_ends_at=now + timedelta(days=3),
    )
    assert event.phase == Phase.UPCOMING
    assert event.phase_at(now + timedelta(days=1, hours=1)) == Phase.OPEN
    # between the close and the judging start: submissions closed, judging not started yet
    assert event.phase_at(now + timedelta(days=2, minutes=30)) == Phase.CLOSED
    assert event.phase_at(now + timedelta(days=2, hours=1)) == Phase.JUDGING
    assert event.phase_at(now + timedelta(days=4)) == Phase.FINISHED


def test_settings_edit(make_event, client_for):
    event = make_event()
    client = client_for(event.organizer)
    data = event_form(name="Renamed", slug=event.slug)
    assert client.post(f"/organizer/events/{event.slug}/", data).status_code == 302
    event.refresh_from_db()
    assert event.name == "Renamed"


def test_team_size_cannot_drop_below_the_largest_team(make_event, make_team, make_user, client_for):
    event = make_event()
    make_team(event, members=[make_user(), make_user()])
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/", event_form(slug=event.slug, max_team_size="2"))
    assert response.status_code == 400


# --- tracks, prizes, questions ---------------------------------------------------------------


def test_add_track_prize_and_question(make_event, client_for):
    event = make_event()
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/track/new", {"name": "Security", "description": "", "order": 0})
    track = Track.objects.get(event=event)
    client.post(f"/organizer/events/{event.slug}/prize/new", {"title": "Best security", "value": "$100", "rank": 1, "track": track.pk})
    client.post(
        f"/organizer/events/{event.slug}/question/new",
        {"prompt": "Pick one", "kind": "choice", "choices": "a\nb", "order": 0, "help_text": ""},
    )
    assert Prize.objects.get(event=event).track == track
    assert CustomQuestion.objects.get(event=event).choice_list() == ["a", "b"]


def test_duplicate_track_name_is_refused(make_event, client_for):
    event = make_event()
    Track.objects.create(event=event, name="Security")
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/track/new", {"name": "security", "order": 0})
    assert response.status_code == 400


def test_choice_question_needs_two_options(make_event, client_for):
    event = make_event()
    client = client_for(event.organizer)
    response = client.post(
        f"/organizer/events/{event.slug}/question/new", {"prompt": "Pick", "kind": "choice", "choices": "only", "order": 0}
    )
    assert response.status_code == 400


def test_a_track_in_use_can_be_hidden_but_not_deleted(make_event, make_team, client_for):
    event = make_event()
    track = Track.objects.create(event=event, name="Security")
    Project.objects.create(team=make_team(event), name="P", track=track)
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/track/{track.pk}/delete")
    assert Track.objects.filter(pk=track.pk).exists()
    client.post(f"/organizer/events/{event.slug}/track/{track.pk}/visibility", {"hidden": "1"})
    track.refresh_from_db()
    assert track.is_hidden
    assert track not in event.visible_tracks()


def test_an_unused_track_can_be_deleted(make_event, client_for):
    event = make_event()
    track = Track.objects.create(event=event, name="Security")
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/track/{track.pk}/delete")
    assert not Track.objects.filter(pk=track.pk).exists()


def test_an_answered_question_cannot_be_deleted_or_change_kind(make_event, make_team, client_for):
    event = make_event()
    question = CustomQuestion.objects.create(event=event, prompt="Why?", kind="short")
    project = Project.objects.create(team=make_team(event), name="P")
    Answer.objects.create(project=project, question=question, value="because")
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/question/{question.pk}/delete")
    assert CustomQuestion.objects.filter(pk=question.pk).exists()
    client.post(
        f"/organizer/events/{event.slug}/question/{question.pk}",
        {"prompt": "Why, really?", "kind": "checkbox", "order": 0},
    )
    question.refresh_from_db()
    assert question.prompt == "Why, really?" and question.kind == "short"


def test_parts_of_another_event_cannot_be_touched(make_event, client_for):
    mine, theirs = make_event(), make_event()
    track = Track.objects.create(event=theirs, name="Theirs")
    client = client_for(mine.organizer)
    assert client.post(f"/organizer/events/{mine.slug}/track/{track.pk}/delete").status_code == 404
    assert Track.objects.filter(pk=track.pk).exists()


# --- co-organizers -----------------------------------------------------------------------------


def test_add_and_remove_a_co_organizer(make_event, make_user, client_for):
    event = make_event()
    co = make_user()  # any account: organizer is a role in *this* event
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/organizers", {"email": co.email.upper()})
    assert co in event.members_with(Role.ORGANIZER)
    assert client_for(co).get(f"/organizer/events/{event.slug}/").status_code == 200
    link = event.memberships.get(user=co, role=Role.ORGANIZER)
    client.post(f"/organizer/events/{event.slug}/organizers/{link.pk}/remove")
    assert co not in event.members_with(Role.ORGANIZER)


def test_a_competitor_cannot_co_organize_their_own_event(make_event, make_team, client_for):
    event = make_event()
    competitor = make_team(event).captain
    response = client_for(event.organizer).post(
        f"/organizer/events/{event.slug}/organizers", {"email": competitor.email}
    )
    assert response.status_code == 400
    assert b"conflict of interest" in response.content
    assert competitor not in event.members_with(Role.ORGANIZER)


def test_platform_admins_are_not_added_as_organizers(make_event, make_user, client_for):
    event = make_event()
    admin = make_user(role=ADMIN)
    response = client_for(event.organizer).post(
        f"/organizer/events/{event.slug}/organizers", {"email": admin.email}
    )
    assert response.status_code == 400


def test_the_last_organizer_cannot_be_removed(make_event, client_for):
    event = make_event()
    link = event.memberships.get(role=Role.ORGANIZER)
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/organizers/{link.pk}/remove")
    assert event.memberships.filter(role=Role.ORGANIZER).count() == 1


def test_the_database_refuses_min_above_max(make_event):
    event = make_event()
    event.min_team_size = event.max_team_size + 1
    with pytest.raises(IntegrityError), transaction.atomic():
        event.save()


def test_raising_the_minimum_cannot_invalidate_submitted_projects(make_event, make_team, client_for):
    from projects.models import Status

    event = make_event()
    Project.objects.create(team=make_team(event), name="Solo", status=Status.SUBMITTED, submitted_at=timezone.now())
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/", event_form(slug=event.slug, min_team_size="2"))
    assert response.status_code == 400
    event.refresh_from_db()
    assert event.min_team_size == 1


@pytest.mark.parametrize("low, high, shown", [(1, 4, "1–4"), (2, 5, "2–5"), (3, 3, "exactly 3"), (1, 1, "exactly 1")])
def test_team_size_is_shown_as_min_to_max(make_event, low, high, shown):
    event = make_event()
    event.min_team_size, event.max_team_size = low, high
    assert event.team_size_display == shown
