"""Team formation: creation, invite links, joining, leaving, captaincy.

The brief names the refusals that must each have their own message: expired, revoked, max uses
reached, team full, already on a team, judge trying to join, submissions closed. Every one has a
test here that asserts the *specific* wording, because collapsing them into "invalid link" is the
failure mode that makes an invite system infuriating to use.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.db import IntegrityError, transaction

from core import clock
from core.errors import SubmissionsClosed
from core.models import AuditAction, AuditLog
from events.models import Role
from projects.models import ProjectStatus
from teams import services
from teams.models import Team, TeamInvite, TeamMember
from tests.factories import (
    add_team_member,
    make_admin,
    make_event,
    make_invite,
    make_judge,
    make_organizer,
    make_project,
    make_submitted_project,
    make_team,
    make_user,
)


@pytest.fixture
def event(db):
    return make_event(name="Invite Test Event", max_team_size=3)


@pytest.fixture
def team(event):
    return make_team(event, name="Nightshift")


@pytest.fixture
def captain(team):
    return team.captain().user


# --------------------------------------------------------------------------------------
# creating a team
# --------------------------------------------------------------------------------------


def test_creating_a_team_makes_the_creator_captain_and_a_participant(event):
    user = make_user()
    team = services.create_team(actor=user, event=event, name="Fresh Team")

    assert team.captain().user == user
    assert event.memberships.filter(user=user, role=Role.PARTICIPANT).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_CREATED, actor=user).exists()


def test_team_names_do_not_have_to_be_unique(event):
    """The organizer fixture has three teams called StillTrail."""
    services.create_team(actor=make_user(), event=event, name="StillTrail")
    services.create_team(actor=make_user(), event=event, name="StillTrail")
    assert Team.objects.filter(event=event, name="StillTrail").count() == 2


def test_a_user_cannot_create_a_second_team_in_the_same_event(event, team, captain):
    with pytest.raises(Exception) as caught:
        services.create_team(actor=captain, event=event, name="Second Team")
    assert "already on" in str(caught.value)


def test_a_judge_cannot_create_a_team(event):
    judge = make_judge(event)
    with pytest.raises(Exception) as caught:
        services.create_team(actor=judge, event=event, name="Conflicted")
    assert "cannot also compete" in str(caught.value)


def test_an_organizer_cannot_create_a_team(event):
    organizer = make_organizer(event)
    with pytest.raises(Exception) as caught:
        services.create_team(actor=organizer, event=event, name="Conflicted")
    assert "cannot also compete" in str(caught.value)


def test_creating_a_team_is_refused_after_the_deadline(db):
    closed = make_event(open_window=False)
    with pytest.raises(SubmissionsClosed) as caught:
        services.create_team(actor=make_user(), event=closed, name="Too Late")

    assert caught.value.code == "submissions_closed"
    assert Team.objects.filter(event=closed).count() == 0


# --------------------------------------------------------------------------------------
# creating invites
# --------------------------------------------------------------------------------------


def test_the_captain_can_create_an_invite_and_the_token_is_only_hashed(team, captain):
    invite, plaintext = services.create_invite(actor=captain, team=team)

    assert plaintext
    assert invite.token_hash == TeamInvite.hash_token(plaintext)
    assert plaintext not in invite.token_hash
    # Nothing in the audit entry reveals the token.
    entry = AuditLog.objects.get(action=AuditAction.INVITE_CREATED)
    assert plaintext not in str(entry.metadata)
    assert invite.token_hash not in str(entry.metadata)


def test_invite_tokens_are_long_and_unpredictable(team, captain):
    tokens = {services.create_invite(actor=captain, team=team)[1] for _ in range(10)}
    assert len(tokens) == 10
    assert all(len(t) >= 32 for t in tokens)


def test_a_non_captain_member_cannot_create_an_invite(team):
    member = add_team_member(team).user
    with pytest.raises(Exception) as caught:
        services.create_invite(actor=member, team=team)
    assert "captain" in str(caught.value)


def test_an_organizer_cannot_create_an_invite_for_someone_elses_team(event, team):
    """Deliberately excluded: adding members to a team you will judge is a conflict."""
    organizer = make_organizer(event)
    with pytest.raises(Exception) as caught:
        services.create_invite(actor=organizer, team=team)
    assert "captain" in str(caught.value)


def test_invite_expiry_is_validated(team, captain):
    for bad in (0, -1, services.MAX_INVITE_DAYS + 1):
        with pytest.raises(Exception):
            services.create_invite(actor=captain, team=team, expires_in_days=bad)


def test_creating_an_invite_is_refused_after_the_deadline(db):
    closed = make_event(open_window=False)
    team = make_team(closed)
    with pytest.raises(SubmissionsClosed):
        services.create_invite(actor=team.captain().user, team=team)


# --------------------------------------------------------------------------------------
# the seven named refusals
# --------------------------------------------------------------------------------------


def test_an_unknown_token_is_refused_with_a_useful_message(event):
    joiner = make_user()
    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=joiner, plaintext="not-a-real-token")
    assert "not valid" in str(caught.value)


def test_an_expired_invite_names_the_expiry(team, captain):
    invite, plaintext = make_invite(team, expires_in_days=1)
    joiner = make_user()

    with clock.offset_by(dt.timedelta(days=2)):
        with pytest.raises(services.InviteRefused) as caught:
            services.join_via_invite(actor=joiner, plaintext=plaintext)
    assert "expired" in str(caught.value)
    assert not TeamMember.objects.filter(user=joiner).exists()


def test_a_revoked_invite_says_it_was_revoked(team, captain):
    invite, plaintext = make_invite(team)
    services.revoke_invite(actor=captain, invite=invite)

    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=make_user(), plaintext=plaintext)
    assert "revoked" in str(caught.value)


def test_an_exhausted_invite_says_how_many_times_it_was_used(team):
    invite, plaintext = make_invite(team, max_uses=1)

    services.join_via_invite(actor=make_user(), plaintext=plaintext)

    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=make_user(), plaintext=plaintext)
    assert "already been used" in str(caught.value)
    invite.refresh_from_db()
    assert invite.use_count == 1


def test_an_unlimited_invite_can_be_used_repeatedly(event, team):
    invite, plaintext = make_invite(team, max_uses=None)
    # max_team_size is 3 and the captain holds one place, so two more can join.
    services.join_via_invite(actor=make_user(), plaintext=plaintext)
    services.join_via_invite(actor=make_user(), plaintext=plaintext)

    invite.refresh_from_db()
    assert invite.use_count == 2
    assert team.member_count == 3


def test_a_full_team_names_the_limit(event, team):
    invite, plaintext = make_invite(team)
    services.join_via_invite(actor=make_user(), plaintext=plaintext)
    services.join_via_invite(actor=make_user(), plaintext=plaintext)
    assert team.is_full

    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=make_user(), plaintext=plaintext)
    assert "full" in str(caught.value)
    assert str(event.max_team_size) in str(caught.value)


def test_someone_already_on_another_team_is_told_which(event, team):
    other_team = make_team(event, name="Other Crew")
    already = other_team.captain().user

    invite, plaintext = make_invite(team)
    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=already, plaintext=plaintext)

    message = str(caught.value)
    assert "Other Crew" in message
    assert "one team per event" in message


def test_an_existing_member_is_told_they_are_already_in(team, captain):
    invite, plaintext = make_invite(team)
    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=captain, plaintext=plaintext)
    assert "already a member" in str(caught.value)


def test_a_judge_of_the_event_cannot_join_a_team(event, team):
    judge = make_judge(event)
    invite, plaintext = make_invite(team)

    with pytest.raises(Exception) as caught:
        services.join_via_invite(actor=judge, plaintext=plaintext)
    assert "judge or organizer" in str(caught.value)
    assert not TeamMember.objects.filter(user=judge).exists()


def test_an_organizer_of_the_event_cannot_join_a_team(event, team):
    organizer = make_organizer(event)
    invite, plaintext = make_invite(team)
    with pytest.raises(Exception) as caught:
        services.join_via_invite(actor=organizer, plaintext=plaintext)
    assert "judge or organizer" in str(caught.value)


def test_joining_is_refused_after_the_deadline(db):
    closed = make_event(open_window=False)
    team = make_team(closed)
    # Built directly, since creating one through the service would itself be refused.
    invite, plaintext = make_invite(team)

    with pytest.raises(SubmissionsClosed) as caught:
        services.join_via_invite(actor=make_user(), plaintext=plaintext)
    assert caught.value.code == "submissions_closed"


def test_every_refusal_produces_a_different_message(event, team, captain):
    """The point of the whole set: these must not collapse into one unhelpful sentence."""
    messages = set()

    # unknown
    with pytest.raises(services.InviteRefused) as c1:
        services.join_via_invite(actor=make_user(), plaintext="nope")
    messages.add(str(c1.value))

    # revoked
    revoked, revoked_plain = make_invite(team, revoked=True)
    with pytest.raises(services.InviteRefused) as c2:
        services.join_via_invite(actor=make_user(), plaintext=revoked_plain)
    messages.add(str(c2.value))

    # already a member
    live, live_plain = make_invite(team)
    with pytest.raises(services.InviteRefused) as c3:
        services.join_via_invite(actor=captain, plaintext=live_plain)
    messages.add(str(c3.value))

    # exhausted
    spent, spent_plain = make_invite(team, max_uses=1)
    services.join_via_invite(actor=make_user(), plaintext=spent_plain)
    with pytest.raises(services.InviteRefused) as c4:
        services.join_via_invite(actor=make_user(), plaintext=spent_plain)
    messages.add(str(c4.value))

    assert len(messages) == 4


def test_a_refused_join_is_audited(team, captain):
    invite, plaintext = make_invite(team, revoked=True)
    with pytest.raises(services.InviteRefused):
        services.join_via_invite(actor=make_user(), plaintext=plaintext)

    assert AuditLog.objects.filter(action=AuditAction.INVITE_REFUSED).exists()


# --------------------------------------------------------------------------------------
# joining
# --------------------------------------------------------------------------------------


def test_joining_registers_the_user_as_a_participant(event, team):
    invite, plaintext = make_invite(team)
    joiner = make_user()

    services.join_via_invite(actor=joiner, plaintext=plaintext)

    assert TeamMember.objects.filter(team=team, user=joiner, is_captain=False).exists()
    assert event.memberships.filter(user=joiner, role=Role.PARTICIPANT).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_JOINED, actor=joiner).exists()


def test_the_database_is_the_final_arbiter_of_one_team_per_event(event, team):
    """Belt and braces on the service check: the unique index must also refuse."""
    other = make_team(event, name="Elsewhere")
    joiner = other.captain().user

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TeamMember.objects.create(team=team, user=joiner, event=event)


# --------------------------------------------------------------------------------------
# leaving
# --------------------------------------------------------------------------------------


def test_a_non_last_captain_must_transfer_captaincy_first(team, captain):
    add_team_member(team)

    with pytest.raises(Exception) as caught:
        services.leave_team(actor=captain, team=team)
    assert "Transfer captaincy" in str(caught.value)
    assert TeamMember.objects.filter(team=team, user=captain).exists()


def test_a_non_captain_member_can_leave(team):
    member = add_team_member(team).user
    result = services.leave_team(actor=member, team=team)

    assert result["team_deleted"] is False
    assert not TeamMember.objects.filter(team=team, user=member).exists()
    assert Team.objects.filter(pk=team.pk).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_LEFT, actor=member).exists()


def test_the_last_member_leaving_deletes_the_team_and_its_draft(event, team, captain):
    draft = make_project(team, status=ProjectStatus.DRAFT)

    result = services.leave_team(actor=captain, team=team)

    assert result["team_deleted"] is True
    assert not Team.objects.filter(pk=team.pk).exists()
    assert not type(draft).objects.filter(pk=draft.pk).exists()
    # Their participation ends with the team.
    assert not event.memberships.filter(user=captain, role=Role.PARTICIPANT).exists()
    assert AuditLog.objects.filter(action=AuditAction.TEAM_DELETED).exists()


def test_leaving_is_refused_if_it_would_orphan_a_submitted_project(team, captain):
    make_submitted_project(team)

    with pytest.raises(Exception) as caught:
        services.leave_team(actor=captain, team=team)

    message = str(caught.value)
    assert "submitted project" in message
    assert Team.objects.filter(pk=team.pk).exists()
    assert TeamMember.objects.filter(team=team, user=captain).exists()


def test_leaving_is_refused_after_the_deadline(db):
    closed = make_event(open_window=False)
    team = make_team(closed)
    with pytest.raises(SubmissionsClosed):
        services.leave_team(actor=team.captain().user, team=team)


def test_leaving_a_team_you_are_not_on_is_refused(team):
    with pytest.raises(Exception) as caught:
        services.leave_team(actor=make_user(), team=team)
    assert "not a member" in str(caught.value)


# --------------------------------------------------------------------------------------
# captaincy
# --------------------------------------------------------------------------------------


def test_captaincy_transfer_moves_the_flag_exactly_once(team, captain):
    member = add_team_member(team).user

    services.transfer_captaincy(actor=captain, team=team, new_captain=member)

    assert team.members.filter(is_captain=True).count() == 1
    assert team.captain().user == member
    assert AuditLog.objects.filter(action=AuditAction.CAPTAIN_TRANSFERRED).exists()


def test_only_the_captain_can_transfer_captaincy(team, captain):
    member = add_team_member(team).user
    third = add_team_member(team).user

    with pytest.raises(Exception) as caught:
        services.transfer_captaincy(actor=member, team=team, new_captain=third)
    assert "current captain" in str(caught.value)


def test_captaincy_cannot_be_given_to_a_non_member(team, captain):
    with pytest.raises(Exception) as caught:
        services.transfer_captaincy(actor=captain, team=team, new_captain=make_user())
    assert "not a member" in str(caught.value)


def test_transfer_then_leave_is_the_supported_path_for_a_captain(team, captain):
    member = add_team_member(team).user

    services.transfer_captaincy(actor=captain, team=team, new_captain=member)
    result = services.leave_team(actor=captain, team=team)

    assert result["team_deleted"] is False
    assert team.captain().user == member


# --------------------------------------------------------------------------------------
# the invite description helper the page and the POST share
# --------------------------------------------------------------------------------------


def test_the_page_and_the_write_path_use_the_same_rules(team, captain):
    """`describe_invite_refusal` is what the GET renders and what the POST re-checks.

    One set of conditions, so nobody is shown a button that was always going to fail.
    """
    invite, plaintext = make_invite(team, revoked=True)

    described = services.describe_invite_refusal(invite=invite, user=make_user())
    assert described

    with pytest.raises(services.InviteRefused) as caught:
        services.join_via_invite(actor=make_user(), plaintext=plaintext)
    assert str(caught.value) == described


def test_an_anonymous_visitor_is_not_refused_merely_for_being_anonymous(team):
    """They should be invited to sign in, not told the link is broken."""
    invite, _ = make_invite(team)
    assert services.describe_invite_refusal(invite=invite, user=None) is None
