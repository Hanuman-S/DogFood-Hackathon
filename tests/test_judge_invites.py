"""Judge invite links: one-time, bound to the invited email, shown once, and audited."""

from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from accounts.models import User, digest_token
from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import EventMembership, JudgeInvite, JudgeTrack, Track

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "judge-horse-battery-9"


@pytest.fixture
def event(make_event):
    event = make_event()
    Track.objects.create(event=event, name="Security")
    Track.objects.create(event=event, name="Open data")
    return event


def create_invite(client, event, email="new.judge@example.org", tracks=()):
    """Post the judges form's 'invite by link' button; return (response, link path)."""
    response = client.post(
        f"/organizer/events/{event.slug}/judges/invite",
        {"email": email, "tracks": [t.pk for t in tracks]},
    )
    path = None
    if response.status_code == 200 and b"judge-invite-link" in response.content:
        link = response.content.decode().split('id="judge-invite-link">')[1].split("<")[0]
        path = link.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
    return response, path


def accept_as_new_account(path, name="Grace Hopper", password=NEW_PASSWORD):
    client = Client()
    response = client.post(path, {"name": name, "password1": password, "password2": password})
    return client, response


# --- the organizer's side ---------------------------------------------------------------------------


def test_the_link_is_shown_once_and_only_its_hash_is_stored(event, client_for):
    response, path = create_invite(client_for(event.organizer), event)
    assert response.status_code == 200 and path.startswith("/invite/judge/jinv_")
    token = path.rsplit("/", 1)[1]
    invite = JudgeInvite.objects.get()
    assert invite.digest == digest_token(token) and token not in invite.digest
    assert invite.expires_at - timezone.now() > timedelta(days=6, hours=23)
    assert AuditLog.objects.filter(action=AuditAction.JUDGE_INVITED, detail__email="new.judge@example.org").exists()
    # the event page lists it as pending, without the link
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/").content.decode()
    assert "new.judge@example.org" in page and "pending" in page and token not in page


def test_only_this_events_organizers_can_create_invites(event, client_for, make_user):
    for user, status in [(make_user(role=Role.ORGANIZER), 404), (make_user(role=Role.JUDGE), 403)]:
        response, _ = create_invite(client_for(user), event)
        assert response.status_code == status
    assert not JudgeInvite.objects.exists()


def test_a_competitor_or_an_existing_judge_cannot_be_invited(event, client_for, make_team, make_user):
    competitor = make_user(email="competitor@example.org")
    make_team(event, captain=competitor)
    judge = make_user(email="judge@example.org")
    EventMembership.objects.create(event=event, user=judge, role=Role.JUDGE)
    client = client_for(event.organizer)
    for email, sentence in [("competitor@example.org", b"conflict of interest"), ("judge@example.org", b"already a judge")]:
        response, path = create_invite(client, event, email=email)
        assert response.status_code == 400 and sentence in response.content and path is None
    assert not JudgeInvite.objects.exists()


def test_inviting_again_cancels_the_old_link(event, client_for):
    client = client_for(event.organizer)
    _, first = create_invite(client, event)
    _, second = create_invite(client, event)
    assert Client().get(first).status_code == 410
    assert Client().get(second).status_code == 200


def test_a_revoked_link_stops_working(event, client_for):
    client = client_for(event.organizer)
    _, path = create_invite(client, event)
    invite = JudgeInvite.objects.get()
    client.post(f"/organizer/events/{event.slug}/judges/invites/{invite.pk}/revoke")
    assert Client().get(path).status_code == 410
    assert AuditLog.objects.filter(action=AuditAction.JUDGE_INVITE_REVOKED).exists()


# --- the invitee's side -------------------------------------------------------------------------------


def test_a_new_person_creates_their_account_from_the_link_and_becomes_a_judge(event, client_for):
    security = event.tracks.get(name="Security")
    _, path = create_invite(client_for(event.organizer), event, tracks=[security])
    page = Client().get(path)
    assert page.status_code == 200 and b"create account and accept" in page.content
    client, response = accept_as_new_account(path)
    assert response.status_code == 302 and response["Location"] == "/judge/"
    user = User.objects.get(email="new.judge@example.org")
    membership = EventMembership.objects.get(user=user, event=event)
    assert membership.role == Role.JUDGE and membership.added_by == event.organizer
    assert list(JudgeTrack.objects.filter(membership=membership).values_list("track__name", flat=True)) == ["Security"]
    assert client.get("/judge/").status_code == 200  # logged in, in the judge portal
    assert JudgeInvite.objects.get().accepted_by == user
    assert AuditLog.objects.filter(action=AuditAction.JUDGE_INVITE_ACCEPTED, actor=user).exists()


def test_the_link_works_once(event, client_for):
    _, path = create_invite(client_for(event.organizer), event)
    accept_as_new_account(path)
    assert Client().get(path).status_code == 410
    _, again = accept_as_new_account(path, name="Someone else")
    assert again.status_code == 410
    assert EventMembership.objects.filter(event=event, role=Role.JUDGE).count() == 1


def test_the_email_on_the_link_cannot_be_changed(event, client_for):
    _, path = create_invite(client_for(event.organizer), event)
    Client().post(path, {"name": "Mallory", "email": "mallory@example.org",
                         "password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert not User.objects.filter(email="mallory@example.org").exists()
    assert User.objects.get(email="new.judge@example.org").name == "Mallory"


def test_an_existing_account_logs_in_then_accepts(event, client_for, make_user):
    existing = make_user(email="ada@example.org")
    _, path = create_invite(client_for(event.organizer), event, email="ada@example.org")
    page = Client().get(path)
    assert b"log in" in page.content and b"create account" not in page.content
    client = client_for(existing)
    assert client.post(path).status_code == 302
    assert EventMembership.objects.filter(event=event, user=existing, role=Role.JUDGE).exists()


def test_someone_else_signed_in_cannot_use_the_link(event, client_for, make_user):
    _, path = create_invite(client_for(event.organizer), event, email="ada@example.org")
    intruder = make_user(email="eve@example.org")
    client = client_for(intruder)
    assert b"this invite is for ada@example.org" in client.get(path).content
    client.post(path)
    assert not EventMembership.objects.filter(event=event, user=intruder, role=Role.JUDGE).exists()
    refusal = AuditLog.objects.get(action=AuditAction.JUDGE_INVITE_REFUSED)
    assert refusal.actor == intruder and "ada@example.org" in refusal.detail["reason"]
    assert judge_invite_is_open(path)


def judge_invite_is_open(path):
    return Client().get(path).status_code == 200


def test_joining_a_team_after_being_invited_blocks_accepting(event, client_for, make_user, make_team):
    """Conflict of interest is checked again at acceptance, not just when the link was made."""
    person = make_user(email="ada@example.org")
    _, path = create_invite(client_for(event.organizer), event, email="ada@example.org")
    make_team(event, captain=person)
    client = client_for(person)
    assert b"conflict of interest" in client.get(path).content
    client.post(path)
    assert not EventMembership.objects.filter(event=event, user=person, role=Role.JUDGE).exists()
    assert AuditLog.objects.filter(action=AuditAction.JUDGE_INVITE_REFUSED).exists()


def test_an_expired_or_made_up_link_is_refused(event, client_for):
    _, path = create_invite(client_for(event.organizer), event)
    JudgeInvite.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert b"expired" in Client().get(path).content
    _, response = accept_as_new_account(path)
    assert response.status_code == 410 and not User.objects.filter(email="new.judge@example.org").exists()
    assert Client().get("/invite/judge/jinv_madeup").status_code == 404


@override_settings(ALLOW_SIGNUP=False)
def test_an_invite_works_even_when_public_sign_up_is_closed(event, client_for):
    _, path = create_invite(client_for(event.organizer), event)
    assert Client().get("/signup").status_code == 403
    _, response = accept_as_new_account(path)
    assert response.status_code == 302
    assert EventMembership.objects.filter(event=event, role=Role.JUDGE, user__email="new.judge@example.org").exists()


def test_a_weak_password_is_refused_and_nothing_is_created(event, client_for):
    _, path = create_invite(client_for(event.organizer), event)
    _, response = accept_as_new_account(path, password="12345")
    assert response.status_code == 400
    assert not User.objects.filter(email="new.judge@example.org").exists()
    assert judge_invite_is_open(path)
