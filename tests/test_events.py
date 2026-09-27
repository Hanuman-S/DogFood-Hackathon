"""Events: creation, who may manage them, visibility, and their tracks, prizes and questions."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import CustomQuestion, Event, Phase, Prize, Track
from projects.models import Answer, Project

pytestmark = pytest.mark.django_db


def event_form(**overrides):
    data = {
        "name": "Spring Hack 2027",
        "slug": "",
        "tagline": "48 hours",
        "description": "## Hello",
        "starts_at": "2027-03-01T09:00",
        "submissions_open_at": "2027-03-01T09:00",
        "submissions_close_at": "2027-03-03T09:00",
        "judging_ends_at": "2027-03-06T09:00",
        "min_team_size": "1",
        "max_team_size": "4",
    }
    data.update(overrides)
    return data


def test_organizer_creates_an_unpublished_event_they_manage(make_user, client_for):
    organizer = make_user(role=Role.ORGANIZER)
    client = client_for(organizer)
    response = client.post("/organizer/events/new", event_form())
    assert response.status_code == 302 and response["Location"] == "/organizer/events/spring-hack-2027/"
    event = Event.objects.get(slug="spring-hack-2027")
    assert not event.is_published
    assert list(event.organizers.all()) == [organizer]
    assert event.submissions_close_at.isoformat() == "2027-03-03T09:00:00+00:00"  # typed as UTC
    assert AuditLog.objects.filter(action=AuditAction.EVENT_CREATED, subject=event.slug).exists()


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"submissions_close_at": "2027-02-28T09:00"}, "submissions_close_at"),
        ({"judging_ends_at": "2027-03-02T09:00"}, "judging_ends_at"),
        ({"starts_at": "2027-03-04T09:00"}, "starts_at"),
        ({"max_team_size": "0"}, "max_team_size"),
        ({"max_team_size": "21"}, "max_team_size"),
        ({"min_team_size": "0"}, "min_team_size"),
        ({"min_team_size": "5"}, "min_team_size"),  # larger than the max of 4
    ],
)
def test_nonsense_dates_and_sizes_are_refused(make_user, client_for, overrides, field):
    client = client_for(make_user(role=Role.ORGANIZER))
    response = client.post("/organizer/events/new", event_form(**overrides))
    assert response.status_code == 400
    assert field in response.context["form"].errors
    assert not Event.objects.exists()


def test_the_database_refuses_an_inverted_window(make_event):
    event = make_event()
    event.submissions_close_at = event.submissions_open_at - timedelta(hours=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        event.save()


def test_slug_must_be_unique(make_event, make_user, client_for):
    make_event(slug="taken")
    client = client_for(make_user(role=Role.ORGANIZER))
    assert client.post("/organizer/events/new", event_form(slug="taken")).status_code == 400


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_organizers_and_admins_create_events(make_user, client_for, role):
    client = client_for(make_user(role=role))
    assert client.post("/organizer/events/new", event_form()).status_code == 403
    assert not Event.objects.exists()


def test_other_organizers_cannot_see_or_edit_an_event(make_event, make_user, client_for):
    event = make_event(published=False)
    stranger = client_for(make_user(role=Role.ORGANIZER))
    assert stranger.get(f"/organizer/events/{event.slug}/").status_code == 404
    assert stranger.post(f"/organizer/events/{event.slug}/publish", {"publish": "1"}).status_code == 404
    event.refresh_from_db()
    assert not event.is_published


def test_admins_manage_every_event(make_event, make_user, client_for):
    event = make_event()
    admin = client_for(make_user(role=Role.ADMIN))
    assert admin.get(f"/organizer/events/{event.slug}/").status_code == 200


def test_unpublished_events_are_invisible_to_the_public(make_event, make_user, client_for):
    event = make_event(published=False)
    assert Client().get(f"/events/{event.slug}").status_code == 404
    assert Client().get("/api/events").json()["events"] == []
    assert client_for(event.organizer).get(f"/events/{event.slug}").status_code == 200  # preview


def test_publishing_makes_it_public_and_is_audited(make_event, client_for):
    event = make_event(published=False)
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/publish", {"publish": "1"})
    assert Client().get(f"/events/{event.slug}").status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.EVENT_PUBLISHED).exists()


def test_phase_is_computed_from_the_dates(make_event):
    now = timezone.now()
    event = make_event(
        submissions_open_at=now + timedelta(days=1), submissions_close_at=now + timedelta(days=2),
        starts_at=now, judging_ends_at=now + timedelta(days=3),
    )
    assert event.phase == Phase.UPCOMING
    assert event.phase_at(now + timedelta(days=1, hours=1)) == Phase.OPEN
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
    co = make_user(role=Role.ORGANIZER)
    client = client_for(event.organizer)
    client.post(f"/organizer/events/{event.slug}/organizers", {"email": co.email.upper()})
    assert co in event.organizers.all()
    assert client_for(co).get(f"/organizer/events/{event.slug}/").status_code == 200
    link = event.organizer_links.get(user=co)
    client.post(f"/organizer/events/{event.slug}/organizers/{link.pk}/remove")
    assert co not in event.organizers.all()


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_organizer_accounts_can_co_organize(make_event, make_user, client_for, role):
    event = make_event()
    other = make_user(role=role)
    response = client_for(event.organizer).post(f"/organizer/events/{event.slug}/organizers", {"email": other.email})
    assert response.status_code == 400
    assert other not in event.organizers.all()


def test_the_last_organizer_cannot_be_removed(make_event, client_for):
    event = make_event()
    link = event.organizer_links.get()
    client_for(event.organizer).post(f"/organizer/events/{event.slug}/organizers/{link.pk}/remove")
    assert event.organizer_links.count() == 1


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
