"""Tests for `manage.py seed_demo`.

Two things matter most here:

1. **DEMO_MODE=0 must produce nothing.** No known password, no fixed token, anywhere. That is
   the difference between a convenient demo and a portal that ships with a backdoor.
2. **The fixed tokens must be exactly what `.dogfood.toml` advertises**, and must survive a
   re-run, because the acceptance checker's config is written against them.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.models import ApiToken, User
from core import clock
from core.deadlines import assert_submissions_open, submissions_are_open
from events.models import CustomQuestion, Event, EventMembership, Prize, QuestionKind, Role, Track
from projects.models import Project, ProjectStatus
from teams.models import Team, TeamInvite, TeamMember

DEMO_PASSWORD = "dogfood-demo"

# Exactly the plaintext values docker-compose.yml defaults to, and therefore exactly what
# .dogfood.toml must carry. If a default is changed in one place and not the other, the
# acceptance checker silently starts failing on authentication instead of on the thing it means
# to test -- so both are pinned here.
EXPECTED_TOKENS = {
    "DEMO_TOKEN_ORGANIZER": "dogfood-demo-organizer-token-do-not-use-in-production",
    "DEMO_TOKEN_JUDGE_A": "dogfood-demo-judge-a-token-do-not-use-in-production",
    "DEMO_TOKEN_JUDGE_B": "dogfood-demo-judge-b-token-do-not-use-in-production",
    "DEMO_TOKEN_PARTICIPANT": "dogfood-demo-participant-token-do-not-use-in-production",
}


@pytest.fixture
def seeded(imported, monkeypatch):
    """Run seed_demo on top of the imported fixture, with DEMO_MODE on."""
    for name, value in EXPECTED_TOKENS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DEMO_INVITE_TOKEN", "test-invite-token")
    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")
    yield


# --------------------------------------------------------------------------------------
# the DEMO_MODE=0 guard
# --------------------------------------------------------------------------------------


def test_seed_demo_refuses_to_run_when_demo_mode_is_off(imported):
    """The guard lives in the command, not in the entrypoint, so calling it directly still
    refuses."""
    with override_settings(DEMO_MODE=False):
        with pytest.raises(CommandError, match="DEMO_MODE=0"):
            call_command("seed_demo", "--quiet")

    # Nothing was created.
    assert not User.objects.filter(email="admin@example.org").exists()
    assert not Event.objects.filter(external_id="demo_live").exists()
    assert ApiToken.objects.count() == 0


def test_with_demo_mode_off_no_imported_account_can_be_logged_into(imported, client):
    for user in User.objects.all()[:10]:
        assert not user.has_usable_password()
    assert client.login(email="priya1@example.org", password=DEMO_PASSWORD) is False


# --------------------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------------------


def test_the_four_named_demo_accounts_exist_with_the_right_powers(seeded):
    admin = User.objects.get(email="admin@example.org")
    assert admin.is_platform_admin and admin.is_staff and admin.is_superuser

    organizer = User.objects.get(email="organizer@example.org")
    assert organizer.can_create_events
    assert not organizer.is_platform_admin

    # The two judges the brief names, by fixture id.
    assert User.objects.get(external_id="jdg_02").email == "wei.lindqvist@example.org"
    assert User.objects.get(external_id="jdg_26").email == "jonas.vogel@example.org"


def test_the_organizer_organizes_both_events(seeded):
    organizer = User.objects.get(email="organizer@example.org")
    slugs = set(
        EventMembership.objects.filter(user=organizer, role=Role.ORGANIZER).values_list(
            "event__slug", flat=True
        )
    )
    assert slugs == {"sample-hack-2026", "dogfood-live-demo"}


def test_every_demo_and_imported_account_can_log_in_with_the_published_password(seeded, client):
    for email in (
        "admin@example.org",
        "organizer@example.org",
        "wei.lindqvist@example.org",
        "jonas.vogel@example.org",
        "priya1@example.org",
    ):
        client.logout()
        assert client.login(email=email, password=DEMO_PASSWORD), email


def test_the_checker_participant_is_the_captain_of_fixture_team_tm_01(seeded):
    """This is what lets the acceptance checker's closed-event POST be a real attempt: the
    account it posts as genuinely belongs to a team in the closed event."""
    priya = User.objects.get(email="priya1@example.org")
    membership = TeamMember.objects.get(user=priya, event__external_id="evt_01")
    assert membership.team.external_id == "tm_01"
    assert membership.is_captain


def test_a_real_signup_keeps_its_own_password(seeded):
    """Demo mode resets imported accounts, not accounts a person created on this portal."""
    User.objects.create_user(
        email="real@person.example", display_name="Real", password="a-genuinely-private-1"
    )
    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")

    user = User.objects.get(email="real@person.example")
    assert user.check_password("a-genuinely-private-1")
    assert not user.check_password(DEMO_PASSWORD)


# --------------------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------------------


def test_tokens_are_issued_for_the_four_checker_roles(seeded):
    expected_owner = {
        EXPECTED_TOKENS["DEMO_TOKEN_ORGANIZER"]: "organizer@example.org",
        EXPECTED_TOKENS["DEMO_TOKEN_JUDGE_A"]: "wei.lindqvist@example.org",
        EXPECTED_TOKENS["DEMO_TOKEN_JUDGE_B"]: "jonas.vogel@example.org",
        EXPECTED_TOKENS["DEMO_TOKEN_PARTICIPANT"]: "priya1@example.org",
    }
    for plaintext, email in expected_owner.items():
        token = ApiToken.objects.get(token_hash=ApiToken.hash_token(plaintext))
        assert token.user.email == email
        assert token.revoked_at is None


def test_tokens_are_stored_hashed_even_though_their_value_is_fixed(seeded):
    """Demo mode changes where the secret comes from, never how it is stored."""
    plaintext = EXPECTED_TOKENS["DEMO_TOKEN_JUDGE_A"]
    for token in ApiToken.objects.all():
        assert token.token_hash != plaintext
        assert len(token.token_hash) == 64
    row = ApiToken.objects.filter(token_hash=ApiToken.hash_token(plaintext)).values().first()
    assert plaintext not in str(row)


def test_reseeding_keeps_the_same_token_values(seeded):
    """`.dogfood.toml` must stay valid across `docker compose down -v` and every restart."""
    before = set(ApiToken.objects.values_list("token_hash", flat=True))
    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")
    assert set(ApiToken.objects.values_list("token_hash", flat=True)) == before


def test_a_revoked_demo_token_is_restored_on_reseed(seeded):
    """Deliberate, and only in demo mode: the value is fixed, so the row cannot be recreated,
    and leaving it revoked would silently break the acceptance checker."""
    plaintext = EXPECTED_TOKENS["DEMO_TOKEN_JUDGE_A"]
    token = ApiToken.objects.get(token_hash=ApiToken.hash_token(plaintext))
    token.revoke()
    assert token.revoked_at is not None

    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")

    token.refresh_from_db()
    assert token.revoked_at is None


# --------------------------------------------------------------------------------------
# the open demo event
# --------------------------------------------------------------------------------------


def test_the_demo_event_is_open_while_the_fixture_event_stays_closed(seeded):
    """Both facts matter. The closed event is what the acceptance checker probes; the open one is
    what makes a draft-edit-submit demo possible at all."""
    demo = Event.objects.get(external_id="demo_live")
    fixture = Event.objects.get(external_id="evt_01")

    assert submissions_are_open(demo) is True
    assert_submissions_open(demo)  # does not raise

    assert submissions_are_open(fixture) is False
    assert clock.now() > fixture.submissions_close_at


def test_the_demo_event_window_is_relative_to_seed_time(seeded):
    demo = Event.objects.get(external_id="demo_live")
    now = clock.now()
    assert demo.submissions_open_at < now < demo.submissions_close_at
    assert (demo.submissions_close_at - now).days >= 6


def test_the_demo_event_mirrors_the_fixture_tracks(seeded):
    demo = Event.objects.get(external_id="demo_live")
    fixture = Event.objects.get(external_id="evt_01")
    assert set(Track.objects.filter(event=demo).values_list("name", flat=True)) == set(
        Track.objects.filter(event=fixture).values_list("name", flat=True)
    )
    assert Track.objects.filter(event=demo).count() == 8


def test_the_demo_event_has_prizes_and_three_kinds_of_custom_question(seeded):
    demo = Event.objects.get(external_id="demo_live")
    assert Prize.objects.filter(event=demo).count() == 3

    questions = CustomQuestion.objects.filter(event=demo)
    assert questions.count() == 3
    assert set(questions.values_list("kind", flat=True)) == {
        QuestionKind.LONG_TEXT,
        QuestionKind.BOOLEAN,
        QuestionKind.CHOICE,
    }
    # Exactly one required question, so submit-time validation is demonstrable.
    assert questions.filter(required=True).count() == 1
    assert questions.get(required=True).kind == QuestionKind.LONG_TEXT
    # The choice question has usable options.
    assert len(questions.get(kind=QuestionKind.CHOICE).choices) >= 2


def test_the_demo_event_has_a_team_with_a_draft_project_and_a_live_invite(seeded):
    demo = Event.objects.get(external_id="demo_live")
    team = Team.objects.get(external_id="demo_live:team")
    assert team.event_id == demo.pk

    project = Project.objects.get(external_id="demo_live:project")
    assert project.status == ProjectStatus.DRAFT
    assert project.submitted_at is None
    # A draft is never in the gallery, in either event.
    assert not Project.objects.gallery_visible().filter(pk=project.pk).exists()

    invite = TeamInvite.objects.get(team=team)
    assert invite.state == "live"
    assert invite.token_hash == TeamInvite.hash_token("test-invite-token")


def test_the_same_person_holds_different_roles_in_different_events(seeded):
    """The point of per-event roles, demonstrated by the seed data itself."""
    priya = User.objects.get(email="priya1@example.org")
    teams = dict(
        TeamMember.objects.filter(user=priya).values_list("event__slug", "team__name")
    )
    assert teams == {"sample-hack-2026": "NorthKiln", "dogfood-live-demo": "Kiln Works"}


# --------------------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------------------


def test_reseeding_creates_no_duplicate_rows(seeded):
    counts = {
        model.__name__: model.objects.count()
        for model in (User, Event, Track, Team, TeamMember, Project, Prize, CustomQuestion, TeamInvite, ApiToken)
    }
    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")
    after = {
        model.__name__: model.objects.count()
        for model in (User, Event, Track, Team, TeamMember, Project, Prize, CustomQuestion, TeamInvite, ApiToken)
    }
    assert counts == after


def test_reseeding_does_not_move_an_open_deadline(seeded):
    """Restarting the container must not shift a window a demo user is working against."""
    demo = Event.objects.get(external_id="demo_live")
    before = (demo.submissions_open_at, demo.submissions_close_at)

    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")

    demo.refresh_from_db()
    assert (demo.submissions_open_at, demo.submissions_close_at) == before


def test_an_expired_demo_window_is_shifted_forward_on_reseed(seeded):
    """The one intentional non-idempotency: a stack left running for weeks should still be able
    to demonstrate submitting."""
    demo = Event.objects.get(external_id="demo_live")
    import datetime as dt

    with clock.frozen_at(demo.submissions_close_at + dt.timedelta(days=3)):
        assert submissions_are_open(demo) is False
        with override_settings(DEMO_MODE=True):
            call_command("seed_demo", "--quiet")
        demo.refresh_from_db()
        assert submissions_are_open(demo) is True


def test_seeding_does_not_touch_the_imported_fixture_event_window(seeded):
    """The fixture event must stay closed forever; that is the whole premise of one T1 check."""
    fixture = Event.objects.get(external_id="evt_01")
    assert fixture.submissions_close_at == clock.parse_utc("2026-03-01T18:00:00Z")

    with override_settings(DEMO_MODE=True):
        call_command("seed_demo", "--quiet")

    fixture.refresh_from_db()
    assert fixture.submissions_close_at == clock.parse_utc("2026-03-01T18:00:00Z")
