"""The permission matrix, driven through real URLs.

`test_permissions.py` tests the policy functions. This module tests what a browser or `curl`
actually receives, because the organizers' rules are explicit that role checks must hold where curl
arrives, not only where a template decides what to draw.

The key distinction asserted throughout: **404 for resources the caller may not know exist**
(another team's draft, someone else's management page), **403 or a redirect for actions they are
merely not allowed to take**.
"""

from __future__ import annotations

import pytest

from events.models import Event
from projects.models import ProjectStatus
from tests.factories import (
    PASSWORD,
    add_team_member,
    make_admin,
    make_event,
    make_judge,
    make_organizer,
    make_project,
    make_submitted_project,
    make_team,
    make_track,
    make_user,
)


@pytest.fixture
def world(db):
    event = make_event(name="Matrix Event")
    other_event = make_event(name="Unrelated Event")

    team = make_team(event, name="Insiders")
    captain = team.captain().user
    teammate = add_team_member(team).user

    rival_team = make_team(event, name="Rivals")

    return {
        "event": event,
        "other_event": other_event,
        "team": team,
        "captain": captain,
        "teammate": teammate,
        "rival": rival_team.captain().user,
        "rival_team": rival_team,
        "judge": make_judge(event),
        "organizer": make_organizer(event),
        "other_organizer": make_organizer(other_event),
        "admin": make_admin(),
        "outsider": make_user(),
    }


def as_user(client, user):
    """Log in, or stay anonymous when `user` is None."""
    client.logout()
    if user is not None:
        client.force_login(user)
    return client


# --------------------------------------------------------------------------------------
# public pages
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "who", [None, "outsider", "captain", "judge", "organizer", "admin"]
)
def test_the_event_page_is_visible_to_everyone(client, world, who):
    as_user(client, world.get(who) if who else None)
    response = client.get(f"/events/{world['event'].slug}")
    assert response.status_code == 200


def test_a_non_public_event_is_404_for_outsiders_but_visible_to_its_members(client, db):
    event = make_event(name="Private Event", gallery_public=False)
    team = make_team(event)
    organizer = make_organizer(event)

    as_user(client, None)
    assert client.get(f"/events/{event.slug}").status_code == 404

    as_user(client, make_user())
    assert client.get(f"/events/{event.slug}").status_code == 404

    as_user(client, team.captain().user)
    assert client.get(f"/events/{event.slug}").status_code == 200

    as_user(client, organizer)
    assert client.get(f"/events/{event.slug}").status_code == 200


# --------------------------------------------------------------------------------------
# the organizer dashboard
# --------------------------------------------------------------------------------------


def test_only_the_events_organizer_and_admins_reach_the_dashboard(client, world):
    url = f"/events/{world['event'].slug}/manage"

    for who in ("organizer", "admin"):
        as_user(client, world[who])
        assert client.get(url).status_code == 200, who

    # 404, not 403: a "forbidden" would confirm the event exists and that you found its
    # management URL.
    for who in ("judge", "captain", "outsider", "other_organizer"):
        as_user(client, world[who])
        assert client.get(url).status_code == 404, who

    as_user(client, None)
    assert client.get(url).status_code == 302  # to the login page


def test_an_organizer_of_another_event_cannot_edit_this_one(client, world):
    as_user(client, world["other_organizer"])
    url = f"/events/{world['event'].slug}/edit"
    assert client.get(url).status_code == 404
    assert client.post(url, {"name": "Hijacked"}).status_code == 404

    world["event"].refresh_from_db()
    assert world["event"].name == "Matrix Event"


def test_the_dashboard_lists_drafts_that_the_gallery_hides(client, world):
    draft = make_project(world["team"], name="Secret Draft")

    as_user(client, world["organizer"])
    body = client.get(f"/events/{world['event'].slug}/manage").content.decode()
    assert "Secret Draft" in body


# --------------------------------------------------------------------------------------
# creating events
# --------------------------------------------------------------------------------------


def test_event_creation_requires_the_grant(client, world):
    as_user(client, world["outsider"])
    assert client.get("/events/new").status_code == 403

    # 403 rather than 404 here: the ability to create events is not a secret, and being told you
    # lack the grant is actionable -- you can ask an admin for it.
    as_user(client, world["captain"])
    assert client.get("/events/new").status_code == 403

    as_user(client, world["admin"])
    assert client.get("/events/new").status_code == 200

    granted = make_user(can_create_events=True)
    as_user(client, granted)
    assert client.get("/events/new").status_code == 200


def test_creating_an_event_makes_the_creator_its_organizer(client, db):
    creator = make_user(can_create_events=True)
    as_user(client, creator)

    response = client.post(
        "/events/new",
        {
            "name": "My New Hackathon",
            "description": "",
            "starts_at": "2026-10-01T09:00",
            "submissions_open_at": "2026-10-01T09:00",
            "submissions_close_at": "2026-10-04T18:00",
            "judging_ends_at": "2026-10-11T18:00",
            "max_team_size": 4,
            "gallery_public": "on",
        },
    )
    assert response.status_code == 302

    event = Event.objects.get(name="My New Hackathon")
    assert event.memberships.filter(user=creator, role="organizer").exists()
    # Typed as UTC, stored as UTC.
    assert event.submissions_close_at.isoformat() == "2026-10-04T18:00:00+00:00"


def test_an_event_with_a_backwards_window_is_rejected_by_the_form(client, db):
    creator = make_user(can_create_events=True)
    as_user(client, creator)

    response = client.post(
        "/events/new",
        {
            "name": "Backwards",
            "starts_at": "2026-10-01T09:00",
            "submissions_open_at": "2026-10-05T09:00",
            "submissions_close_at": "2026-10-02T18:00",
            "max_team_size": 4,
        },
    )
    assert response.status_code == 200
    assert not Event.objects.filter(name="Backwards").exists()


# --------------------------------------------------------------------------------------
# tracks, prizes, questions
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["tracks/new", "prizes/new", "questions/new"])
def test_only_organizers_can_reach_the_management_forms(client, world, suffix):
    url = f"/events/{world['event'].slug}/{suffix}"

    as_user(client, world["organizer"])
    assert client.get(url).status_code == 200

    for who in ("judge", "captain", "outsider", "other_organizer"):
        as_user(client, world[who])
        assert client.get(url).status_code == 404, who


def test_a_participant_cannot_post_a_new_track(client, world):
    """The hidden-button test: the form is not rendered for them, so the POST is what matters."""
    as_user(client, world["captain"])
    response = client.post(
        f"/events/{world['event'].slug}/tracks/new", {"name": "Injected", "order": 0}
    )
    assert response.status_code == 404
    assert not world["event"].tracks.filter(name="Injected").exists()


def test_a_track_with_projects_cannot_be_deleted(client, world):
    track = make_track(world["event"], name="Busy Track")
    make_submitted_project(world["team"], track=track)

    as_user(client, world["organizer"])
    response = client.post(
        f"/events/{world['event'].slug}/tracks/{track.pk}/delete", follow=True
    )
    assert response.status_code == 200
    assert world["event"].tracks.filter(pk=track.pk).exists()
    assert b"cannot be deleted" in response.content


def test_an_unused_track_can_be_deleted(client, world):
    track = make_track(world["event"], name="Idle Track")
    as_user(client, world["organizer"])
    client.post(f"/events/{world['event'].slug}/tracks/{track.pk}/delete")
    assert not world["event"].tracks.filter(pk=track.pk).exists()


# --------------------------------------------------------------------------------------
# teams
# --------------------------------------------------------------------------------------


def test_only_members_organizers_and_admins_see_a_team_page(client, world):
    url = f"/teams/{world['team'].pk}"

    for who in ("captain", "teammate", "organizer", "admin"):
        as_user(client, world[who])
        assert client.get(url).status_code == 200, who

    for who in ("rival", "judge", "outsider", "other_organizer"):
        as_user(client, world[who])
        assert client.get(url).status_code == 404, who

    as_user(client, None)
    assert client.get(url).status_code == 302


def test_a_judge_cannot_post_a_new_team(client, world):
    as_user(client, world["judge"])
    response = client.post(
        f"/events/{world['event'].slug}/teams/new", {"name": "Conflicted Team"}
    )
    # Rendered with an error rather than a redirect, and no team created.
    assert response.status_code in (200, 403, 409)
    assert not world["event"].teams.filter(name="Conflicted Team").exists()


def test_only_the_captain_can_post_an_invite(client, world):
    url = f"/teams/{world['team'].pk}/invites"

    as_user(client, world["teammate"])
    client.post(url, {"expires_in_days": 7}, follow=True)
    assert world["team"].invites.count() == 0

    as_user(client, world["captain"])
    client.post(url, {"expires_in_days": 7}, follow=True)
    assert world["team"].invites.count() == 1


def test_an_outsider_posting_to_a_team_url_gets_404(client, world):
    as_user(client, world["outsider"])
    assert client.post(f"/teams/{world['team'].pk}/invites", {"expires_in_days": 7}).status_code == 404
    assert client.post(f"/teams/{world['team'].pk}/leave").status_code == 404


# --------------------------------------------------------------------------------------
# the invite landing page
# --------------------------------------------------------------------------------------


def test_the_invite_page_is_public_and_explains_itself(client, world):
    from tests.factories import make_invite

    invite, plaintext = make_invite(world["team"])

    as_user(client, None)
    response = client.get(f"/invite/{plaintext}")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Insiders" in body
    # Offers sign-in that returns to this invite, rather than dumping them on the home page.
    assert f"/login?next=/invite/{plaintext}" in body


def test_an_unknown_invite_explains_rather_than_404s(client, db):
    as_user(client, None)
    response = client.get("/invite/definitely-not-a-real-token")
    assert response.status_code == 200
    assert b"not valid" in response.content


def test_posting_an_invite_while_anonymous_redirects_to_login_and_back(client, world):
    from tests.factories import make_invite

    invite, plaintext = make_invite(world["team"])
    as_user(client, None)

    response = client.post(f"/invite/{plaintext}")
    assert response.status_code == 302
    assert response["Location"] == f"/login?next=/invite/{plaintext}"


def test_joining_through_the_page_adds_the_member(client, world):
    from tests.factories import make_invite

    invite, plaintext = make_invite(world["team"])
    joiner = make_user()

    as_user(client, joiner)
    response = client.post(f"/invite/{plaintext}", follow=True)
    assert response.status_code == 200
    assert world["team"].members.filter(user=joiner).exists()


def test_a_refused_join_renders_the_reason_with_a_409(client, world):
    from tests.factories import make_invite

    invite, plaintext = make_invite(world["team"], revoked=True)
    as_user(client, make_user())

    response = client.post(f"/invite/{plaintext}")
    # 409 rather than 200: a refusal that looks like success to a script is a refusal that will be
    # retried forever.
    assert response.status_code == 409
    assert b"revoked" in response.content


# --------------------------------------------------------------------------------------
# memberships
# --------------------------------------------------------------------------------------


def test_an_organizer_can_add_a_judge_by_email(client, world):
    track = make_track(world["event"])
    newcomer = make_user("judge-to-be@example.test")

    as_user(client, world["organizer"])
    client.post(
        f"/events/{world['event'].slug}/members/add",
        {"email": newcomer.email, "role": "judge", "tracks": [track.pk]},
        follow=True,
    )

    membership = world["event"].memberships.get(user=newcomer, role="judge")
    assert list(membership.judge_tracks.values_list("track__name", flat=True)) == [track.name]


def test_adding_a_participant_as_a_judge_is_refused_with_a_message(client, world):
    """The conflict-of-interest rule, surfaced as a sentence rather than a 500."""
    as_user(client, world["organizer"])
    response = client.post(
        f"/events/{world['event'].slug}/members/add",
        {"email": world["captain"].email, "role": "judge"},
        follow=True,
    )
    assert b"already a participant" in response.content
    assert not world["event"].memberships.filter(user=world["captain"], role="judge").exists()


def test_adding_an_unknown_email_says_so(client, world):
    as_user(client, world["organizer"])
    response = client.post(
        f"/events/{world['event'].slug}/members/add",
        {"email": "nobody@nowhere.test", "role": "judge"},
        follow=True,
    )
    assert b"No account exists" in response.content


def test_the_last_organizer_cannot_be_removed(client, world):
    event = world["event"]
    membership = event.memberships.get(user=world["organizer"], role="organizer")

    as_user(client, world["organizer"])
    response = client.post(
        f"/events/{event.slug}/members/{membership.pk}/remove", follow=True
    )
    assert b"only organizer" in response.content
    assert event.memberships.filter(role="organizer").count() == 1


def test_an_organizer_can_be_removed_once_another_exists(client, world):
    event = world["event"]
    second = make_organizer(event)
    membership = event.memberships.get(user=second, role="organizer")

    as_user(client, world["organizer"])
    client.post(f"/events/{event.slug}/members/{membership.pk}/remove", follow=True)
    assert not event.memberships.filter(user=second, role="organizer").exists()


# --------------------------------------------------------------------------------------
# the Django admin
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["outsider", "captain", "judge", "organizer"])
def test_the_django_admin_is_admin_only(client, world, who):
    as_user(client, world[who])
    response = client.get("/admin/", follow=True)
    assert b"Platform administration" not in response.content


def test_the_django_admin_opens_for_a_platform_admin(client, world):
    as_user(client, world["admin"])
    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"Platform administration" in response.content
