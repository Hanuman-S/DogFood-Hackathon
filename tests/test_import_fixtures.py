"""Tests for the organizer fixture import.

These run against the **real** `acceptance/fixtures.json`, not a miniature stand-in. The awkward
cases in that file -- a duplicate submission, three teams with the same name, uneven review
coverage -- are the whole point of it, and a synthetic fixture would test a tidier world than
the one the portal has to survive.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db.models import Count

from accounts.models import User
from core import clock
from core.deadlines import assert_submissions_open
from core.errors import SubmissionsClosed
from events.models import Event, EventMembership, JudgeTrack, Prize, Role, Track
from projects.models import Project, ProjectStatus
from scoring.models import Criterion, Score, ScoreItem
from seed.importer import FixtureImporter
from teams.models import Team, TeamMember

# The organizer file, found relative to this test rather than to the working directory.
FIXTURES_PATH = Path(__file__).resolve().parent.parent / "acceptance" / "fixtures.json"

# `imported` and `fixture_event` come from conftest.py, which loads the fixture once per session
# rather than once per test -- see the docstring there.

# Counts taken from the file itself, asserted here so a silently truncated or swapped fixture is
# caught rather than quietly importing less than it should.
EXPECTED = {
    "tracks": 8,
    "judges": 30,
    "teams": 40,
    "projects": 41,
    "scores": 126,
    "participants": 91,
    "score_items": 378,  # 126 reviews x 3 criteria
    "judge_tracks": 39,  # 21 judges with one track + 9 with two
}


def test_the_organizer_fixture_file_is_present_and_untouched():
    """The import tests are meaningless if this file is missing or has been edited."""
    assert FIXTURES_PATH.exists(), f"organizer fixture missing at {FIXTURES_PATH}"
    import json

    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    assert data["event"]["id"] == "evt_01"
    assert data["event"]["submissions_close"] == "2026-03-01T18:00:00Z"
    assert len(data["projects"]) == EXPECTED["projects"]
    assert len(data["judges"]) == EXPECTED["judges"]
    assert len(data["teams"]) == EXPECTED["teams"]
    assert len(data["tracks"]) == EXPECTED["tracks"]
    assert len(data["scores"]) == EXPECTED["scores"]


# --------------------------------------------------------------------------------------
# counts
# --------------------------------------------------------------------------------------


def test_import_creates_the_expected_row_counts(imported):
    assert Event.objects.count() == 1
    assert Track.objects.count() == EXPECTED["tracks"]
    assert Team.objects.count() == EXPECTED["teams"]
    assert Project.objects.count() == EXPECTED["projects"]
    assert Score.objects.count() == EXPECTED["scores"]
    assert ScoreItem.objects.count() == EXPECTED["score_items"]
    assert EventMembership.objects.filter(role=Role.JUDGE).count() == EXPECTED["judges"]
    assert JudgeTrack.objects.count() == EXPECTED["judge_tracks"]
    assert TeamMember.objects.count() == EXPECTED["participants"]
    assert User.objects.count() == EXPECTED["judges"] + EXPECTED["participants"]


def test_every_project_is_imported_as_submitted(imported):
    assert Project.objects.filter(status=ProjectStatus.SUBMITTED).count() == EXPECTED["projects"]
    assert Project.objects.filter(submitted_at__isnull=True).count() == 0


def test_criteria_are_discovered_from_the_data(imported):
    keys = set(Criterion.objects.values_list("key", flat=True))
    assert keys == {"functionality", "quality", "innovation"}
    for criterion in Criterion.objects.all():
        assert criterion.weight == 1
        assert criterion.min_value == 1
        assert criterion.max_value == 5


# --------------------------------------------------------------------------------------
# the synthesized event timeline
# --------------------------------------------------------------------------------------


def test_event_dates_are_synthesized_from_the_single_fixture_date(imported):
    event = Event.objects.get(external_id="evt_01")
    close = dt.datetime(2026, 3, 1, 18, 0, tzinfo=dt.timezone.utc)

    assert event.submissions_close_at == close
    assert event.submissions_open_at == close - dt.timedelta(hours=72)
    assert event.starts_at == close - dt.timedelta(hours=72)
    assert event.judging_ends_at == close + dt.timedelta(days=10)
    assert event.slug == "sample-hack-2026"


def test_the_report_declares_which_dates_were_synthesized(imported):
    """Honesty requirement: the synthesized values must be labelled, not presented as data."""
    blob = "\n".join(imported.synthesized)
    assert "starts_at" in blob
    assert "submissions_open_at" in blob
    assert "judging_ends_at" in blob
    assert "close - 72h" in blob
    rendered = imported.render()
    assert "synthesized (NOT from the fixture)" in rendered
    assert "absent from the fixture, left empty" in rendered


def test_every_fixture_submission_falls_inside_the_synthesized_window(imported):
    """Confirms the derived window is consistent with the data, not merely well-ordered."""
    event = Event.objects.get(external_id="evt_01")
    for project in Project.objects.all():
        assert event.submissions_open_at <= project.submitted_at < event.submissions_close_at


def test_the_imported_event_is_closed_now_with_no_clock_manipulation(imported):
    """The premise of the acceptance checker's third T1 check.

    The fixture event closed in March 2026, so an honestly seeded portal refuses submissions to
    it today without anything touching a clock.
    """
    event = Event.objects.get(external_id="evt_01")
    assert clock.now() > event.submissions_close_at
    assert event.submissions_open is False
    with pytest.raises(SubmissionsClosed):
        assert_submissions_open(event)


# --------------------------------------------------------------------------------------
# the planted duplicate
# --------------------------------------------------------------------------------------


def test_prj_41_is_flagged_as_a_duplicate_of_prj_07(imported):
    later = Project.objects.get(external_id="prj_41")
    earlier = Project.objects.get(external_id="prj_07")

    assert later.duplicate_of_id == earlier.pk
    # The earlier submission stays canonical.
    assert earlier.duplicate_of_id is None
    assert earlier.submitted_at < later.submitted_at
    # Same team, same title, same repo -- which is why it was flagged.
    assert later.team_id == earlier.team_id
    assert later.name == earlier.name == "Dry Harbour"
    assert later.repo_url == earlier.repo_url


def test_both_duplicate_rows_are_kept_with_their_own_scores(imported):
    """Deleting the later row would destroy the scores attached to it, and the evidence an
    organizer needs to decide what to do about it. T2 decides how to treat the pair."""
    later = Project.objects.get(external_id="prj_41")
    earlier = Project.objects.get(external_id="prj_07")
    assert later.scores.exists()
    assert earlier.scores.exists()
    assert Project.objects.filter(external_id__in=["prj_07", "prj_41"]).count() == 2


def test_exactly_one_duplicate_is_flagged_in_the_whole_fixture(imported):
    duplicates = Project.objects.filter(duplicate_of__isnull=False)
    assert duplicates.count() == 1
    assert duplicates.first().external_id == "prj_41"
    assert len(imported.duplicates) == 1
    assert "prj_41" in imported.duplicates[0] and "prj_07" in imported.duplicates[0]


def test_the_duplicate_is_excluded_from_the_gallery_but_the_canonical_row_is_not(imported):
    visible = set(Project.objects.gallery_visible().values_list("external_id", flat=True))
    assert "prj_07" in visible
    assert "prj_41" not in visible
    assert len(visible) == EXPECTED["projects"] - 1


# --------------------------------------------------------------------------------------
# duplicate team names
# --------------------------------------------------------------------------------------


def test_duplicate_team_names_are_preserved_not_merged(imported):
    """The fixture contains StillTrail three times. Teams are keyed by id, never by name."""
    counts = {
        row["name"]: row["n"]
        for row in Team.objects.values("name").annotate(n=Count("id")).filter(n__gt=1)
    }
    assert counts == {"StillTrail": 3, "AmberSwitch": 2, "OpenSignal": 2}
    assert Team.objects.filter(name="StillTrail").count() == 3
    # Each one is a distinct team with its own fixture id.
    assert Team.objects.filter(name="StillTrail").values("external_id").distinct().count() == 3


# --------------------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------------------


def test_a_second_import_creates_nothing_and_changes_nothing(imported):
    """The entrypoint runs this on every boot, so "idempotent" has to mean literally nothing.

    Not just "no new rows": no UPDATE either, which is why the importer compares before writing
    instead of using `update_or_create`.
    """
    before = {
        model.__name__: model.objects.count()
        for model in (User, Event, Track, Team, TeamMember, Project, Score, ScoreItem, Prize)
    }
    stamps = dict(Project.objects.values_list("external_id", "updated_at"))

    second = FixtureImporter(FIXTURES_PATH).run()

    after = {
        model.__name__: model.objects.count()
        for model in (User, Event, Track, Team, TeamMember, Project, Score, ScoreItem, Prize)
    }
    assert before == after

    for tally in second.tallies.values():
        assert tally.created == 0, f"second run created rows: {tally}"
        assert tally.updated == 0, f"second run updated rows: {tally}"

    # `updated_at` is untouched, which is the observable difference from update_or_create.
    assert dict(Project.objects.values_list("external_id", "updated_at")) == stamps


def test_a_third_run_through_the_management_command_is_also_clean(imported):
    """Covers the CLI wrapper as well as the importer class."""
    before = Project.objects.count()
    call_command("import_fixtures", str(FIXTURES_PATH), "--quiet")
    assert Project.objects.count() == before


def test_an_organizer_extending_a_deadline_survives_a_reimport(imported):
    """The import must not be authoritative, only idempotent.

    The brief explicitly lets an organizer extend `submissions_close_at`. The entrypoint runs
    `import_fixtures` on every boot, so an importer that wrote the fixture value back whenever it
    differed would silently revert that extension on the next container restart -- breaking a T1
    feature and contradicting "seeding never overwrites user-created data".
    """
    event = Event.objects.get(external_id="evt_01")
    extended = event.submissions_close_at + dt.timedelta(days=30)
    Event.objects.filter(pk=event.pk).update(
        submissions_close_at=extended,
        judging_ends_at=extended + dt.timedelta(days=10),
    )

    report = FixtureImporter(FIXTURES_PATH).run()

    event.refresh_from_db()
    assert event.submissions_close_at == extended, "the import reverted an organizer's edit"

    # And it says so, rather than staying silent about the divergence.
    assert report.preserved_total >= 1
    rendered = report.render()
    assert "PRESERVED" in rendered
    assert "submissions_close_at" in rendered
    assert "create-only" in rendered


def test_edits_to_an_imported_project_survive_a_reimport(imported):
    """Same rule one level down: a team's own edits to their project are theirs."""
    project = Project.objects.get(external_id="prj_01")
    Project.objects.filter(pk=project.pk).update(
        name="Glass Signal (renamed by the team)",
        tagline="Our own tagline, thanks.",
    )

    FixtureImporter(FIXTURES_PATH).run()

    project.refresh_from_db()
    assert project.name == "Glass Signal (renamed by the team)"
    assert project.tagline == "Our own tagline, thanks."


def test_sync_mode_deliberately_restores_the_fixture_values(imported):
    """`--sync` is the opt-in escape hatch, for an operator who really does want the fixture to
    win. It exists so that create-only is a default rather than a limitation."""
    event = Event.objects.get(external_id="evt_01")
    original_close = event.submissions_close_at
    extended = original_close + dt.timedelta(days=30)
    # `judging_ends_at` has to move too: the `event_judging_ends_after_submissions_close` check
    # constraint refuses a window that closes after judging ends, which is the constraint doing
    # its job.
    Event.objects.filter(pk=event.pk).update(
        submissions_close_at=extended,
        judging_ends_at=extended + dt.timedelta(days=10),
    )

    report = FixtureImporter(FIXTURES_PATH, sync=True).run()

    event.refresh_from_db()
    assert event.submissions_close_at == original_close
    assert report.tallies["event"].updated == 1
    assert report.preserved_total == 0
    assert "SYNC" in report.render()


def test_sync_is_off_by_default_on_the_management_command(imported):
    """Guards against the flag defaulting the wrong way, which is a silent, destructive failure."""
    event = Event.objects.get(external_id="evt_01")
    extended = event.submissions_close_at + dt.timedelta(days=30)
    Event.objects.filter(pk=event.pk).update(
        submissions_close_at=extended,
        judging_ends_at=extended + dt.timedelta(days=10),
    )

    call_command("import_fixtures", str(FIXTURES_PATH), "--quiet")

    event.refresh_from_db()
    assert event.submissions_close_at == extended


def test_import_does_not_delete_user_created_data(imported):
    """Re-running on a live portal must not wipe anything a real participant made."""
    event = Event.objects.get(external_id="evt_01")
    outsider = User.objects.create_user(email="real@person.example", display_name="Real Person")
    team = Team.objects.create(event=event, name="Created By Hand")
    TeamMember.objects.create(team=team, user=outsider, event=event, is_captain=True)

    FixtureImporter(FIXTURES_PATH).run()

    assert User.objects.filter(email="real@person.example").exists()
    assert Team.objects.filter(name="Created By Hand").exists()


# --------------------------------------------------------------------------------------
# what is imported, and what is deliberately not
# --------------------------------------------------------------------------------------


def test_absent_fixture_fields_are_left_empty_rather_than_invented(imported):
    """The fixture has no descriptions, images, tags, videos or live URLs.

    Filling them with plausible text would misrepresent our content as the organizers' data.
    """
    for project in Project.objects.all():
        assert project.description == ""
        assert project.demo_video_url == ""
        assert project.live_url == ""
        assert not project.thumbnail
        assert project.images.count() == 0
        assert project.project_tags.count() == 0

    for track in Track.objects.all():
        assert track.description == ""

    assert Event.objects.get(external_id="evt_01").description == ""


def test_project_fields_map_from_the_fixture_names(imported):
    project = Project.objects.get(external_id="prj_01")
    assert project.name == "Glass Signal"  # title -> name
    assert project.tagline == "One line of what it does."  # summary -> tagline
    assert project.repo_url == "https://example.org/repo/01"
    assert project.submitted_at == clock.parse_utc("2026-02-27T04:08:00Z")
    assert project.track.external_id == "trk_04"
    assert project.team.external_id == "tm_01"


def test_prizes_are_seeded_and_labelled_as_demo_data(imported):
    """The fixture has no prizes, so the ones that exist must say where they came from."""
    prizes = Prize.objects.all()
    assert prizes.count() == 1 + EXPECTED["tracks"]
    assert prizes.filter(name="Grand Prize", track__isnull=True).count() == 1
    assert prizes.filter(name__startswith="Best in ", track__isnull=False).count() == EXPECTED["tracks"]
    for prize in prizes:
        assert "Demo data" in prize.description


# --------------------------------------------------------------------------------------
# accounts and roles
# --------------------------------------------------------------------------------------


def test_judges_get_judge_memberships_and_their_track_assignments(imported):
    judge = User.objects.get(external_id="jdg_02")
    assert judge.email == "wei.lindqvist@example.org"
    assert judge.display_name == "Wei Lindqvist"

    membership = EventMembership.objects.get(user=judge, role=Role.JUDGE)
    assigned = set(
        JudgeTrack.objects.filter(membership=membership).values_list(
            "track__external_id", flat=True
        )
    )
    assert assigned == {"trk_02", "trk_04"}


def test_no_judge_is_also_a_participant(imported):
    """The conflict-of-interest rule, checked against the real data.

    The fixture's judge and participant email sets are disjoint, so a violation here would mean
    the importer had invented an overlap.
    """
    judge_ids = set(
        EventMembership.objects.filter(role=Role.JUDGE).values_list("user_id", flat=True)
    )
    participant_ids = set(
        EventMembership.objects.filter(role=Role.PARTICIPANT).values_list("user_id", flat=True)
    )
    assert judge_ids & participant_ids == set()


def test_the_first_listed_team_member_is_the_captain(imported):
    """The brief's rule, and the reason priya1 can act for tm_01."""
    team = Team.objects.get(external_id="tm_01")
    captain = team.captain()
    assert captain is not None
    assert captain.user.email == "priya1@example.org"
    assert team.members.filter(is_captain=True).count() == 1


def test_every_team_has_exactly_one_captain(imported):
    for team in Team.objects.all():
        assert team.members.filter(is_captain=True).count() == 1, team.external_id


def test_imported_accounts_cannot_be_logged_into(imported, client):
    """Import alone never grants access. Passwords arrive only from seed_demo in DEMO_MODE."""
    for user in User.objects.all()[:5]:
        assert not user.has_usable_password()
    assert client.login(email="priya1@example.org", password="dogfood-demo") is False


def test_participant_display_names_are_derived_visibly_from_the_email(imported):
    """The fixture gives team members no names, so the local part is used verbatim.

    Deliberately not a plausible-looking invented human name.
    """
    user = User.objects.get(email="priya1@example.org")
    assert user.display_name == "priya1"
    assert user.external_id is None
    assert "local part" in "\n".join(imported.notes)


# --------------------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------------------


def test_the_search_vector_is_populated_for_every_imported_project(imported):
    assert Project.objects.filter(search_vector__isnull=True).count() == 0


def test_imported_projects_are_findable_by_title(imported):
    from django.contrib.postgres.search import SearchQuery

    found = Project.objects.filter(search_vector=SearchQuery("Harbour", config="english"))
    assert {p.external_id for p in found} >= {"prj_07", "prj_41"}


# --------------------------------------------------------------------------------------
# a missing fixture file must not break the boot
# --------------------------------------------------------------------------------------


def test_a_missing_fixtures_file_is_a_warning_not_a_crash(db, tmp_path, capsys):
    """compose bind-mounts the file, but the bare image may not have it.

    A crash-looping container on a missing input would be a much worse failure than an empty
    portal that explains itself.

    Counted as a delta rather than against zero, because whether the session-scoped fixture
    import has already run depends on test ordering, and a test that only passes in one order is
    a test that will fail confusingly later.
    """
    before = Event.objects.count()

    call_command("import_fixtures", str(tmp_path / "nope.json"))

    captured = capsys.readouterr()
    assert "not found" in captured.err
    assert Event.objects.count() == before, "a missing file must not create or remove anything"


def test_the_default_fixture_path_points_where_compose_mounts_it():
    assert settings.FIXTURES_PATH.endswith("fixtures.json")
