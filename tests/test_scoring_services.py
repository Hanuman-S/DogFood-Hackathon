"""Scoring services (S3): the adapter, the judging gate, snapshots, publications, the config lock.

compute_snapshot opens its own REPEATABLE READ transaction and refuses to run inside another,
so every test that calls it uses django_db(transaction=True). That check is never weakened to
suit a test.
"""

import io
import json
import re
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils import timezone
from engine_helpers import FIXTURES, LAB_SEED

from accounts.roles import ADMIN, Role
from core.deadlines import deadline_bypass
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership
from imports.fixtures import import_file
from imports.models import FixtureRef
from projects.models import Project, Status
from scoring import services
from scoring.engine import pipeline
from scoring.engine.config import seed_for
from scoring.engine.io import load_organizer_file
from scoring.errors import (FinalOverrideRefused, InvalidConfig, JudgingOpen, ScoringConfigLocked,
                            SnapshotInsideTransaction)
from scoring.models import Criterion, Publication, ResultSnapshot, Score, ScoreItem, SnapshotImmutable

TX = pytest.mark.django_db(transaction=True)


# --- a small scored event ----------------------------------------------------------------------------

@pytest.fixture
def scored_event(make_event, make_user, make_team):
    """4 submitted projects, 3 judges who each reviewed every project, 3 criteria of weight 1.
    Its submission window is still open (so the projects can be written); tests move the
    database clock with `at(...)`."""
    event = make_event()
    criteria = [Criterion.objects.create(event=event, key=k, label=k, weight=1, order=i)
                for i, k in enumerate(("functionality", "quality", "innovation"))]
    now = timezone.now()
    projects = []
    for i in range(4):
        team = make_team(event)
        projects.append(Project.objects.create(team=team, event=event, name=f"P{i}",
                                               status=Status.SUBMITTED, submitted_at=now))
    judges = []
    for j in range(3):
        user = make_user(role=Role.JUDGE)
        judges.append(EventMembership.objects.create(user=user, event=event, role=Role.JUDGE))
    for j, judge in enumerate(judges):
        for i, project in enumerate(projects):
            score = Score.objects.create(judge=judge, project=project, submitted_at=now)
            for c, criterion in enumerate(criteria):
                ScoreItem.objects.create(score=score, criterion=criterion, value=1 + (i + j + c) % 5)
    event.judges = judges
    event.projects_list = projects
    return event


@pytest.fixture
def at(monkeypatch):
    """Set the database clock the scoring services see."""
    def set_now(when):
        monkeypatch.setattr("scoring.services.db_now", lambda: when)
    return set_now


def phases(event):
    """(name, instant) inside each phase before judging closes, and the last instant before."""
    return [
        ("upcoming", event.submissions_open_at - timedelta(hours=1)),
        ("open", event.submissions_close_at - timedelta(hours=1)),
        ("judging", event.submissions_close_at + timedelta(hours=1)),
        ("one second before judging ends", event.judging_ends_at - timedelta(seconds=1)),
    ]


def refused_count(action=AuditAction.SNAPSHOT_REFUSED):
    return AuditLog.objects.filter(action=action).count()


# --- 1. the gate -----------------------------------------------------------------------------------

@TX
def test_final_refused_in_every_phase_before_judging_ends(scored_event, at):
    for name, when in phases(scored_event):
        at(when)
        with pytest.raises(JudgingOpen) as caught:
            services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
        assert (caught.value.status, caught.value.code) == (409, "judging_open"), name
    assert ResultSnapshot.objects.count() == 0
    assert refused_count() == 4


@TX
def test_final_allowed_at_judging_end_and_after(scored_event, at):
    for when in (scored_event.judging_ends_at, scored_event.judging_ends_at + timedelta(days=30)):
        at(when)
        snapshot = services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
        assert snapshot.kind == "final"
    assert ResultSnapshot.objects.filter(kind="final").count() == 2
    assert AuditLog.objects.filter(action=AuditAction.SNAPSHOT_CREATED).count() == 2


@TX
def test_preview_allowed_in_every_phase(scored_event, at):
    for _, when in phases(scored_event) + [("finished", scored_event.judging_ends_at + timedelta(days=1))]:
        at(when)
        assert services.compute_snapshot(scored_event, "preview", actor=scored_event.organizer).kind == "preview"
    assert ResultSnapshot.objects.filter(kind="preview").count() == 5


def test_judging_closed_is_the_boundary_rule():
    event = Event(judging_ends_at=timezone.now())
    assert services.judging_closed(event, event.judging_ends_at)
    assert not services.judging_closed(event, event.judging_ends_at - timedelta(microseconds=1))


# --- 2. roles ----------------------------------------------------------------------------------------

@TX
def test_participant_and_judge_get_403_for_both_kinds_in_every_phase(scored_event, make_team, make_user, at):
    participant = make_team(scored_event).captain
    judge = scored_event.judges[0].user
    for when in [w for _, w in phases(scored_event)] + [scored_event.judging_ends_at + timedelta(days=1)]:
        at(when)
        for user in (participant, judge):
            for kind in ("preview", "final"):
                with pytest.raises(PermissionDenied):
                    services.compute_snapshot(scored_event, kind, actor=user)
    assert ResultSnapshot.objects.count() == 0
    assert refused_count() == 5 * 2 * 2


@TX
def test_organizer_of_another_event_is_refused_and_admin_allowed(scored_event, make_event, make_user, at):
    at(scored_event.judging_ends_at)
    other = make_event().organizer
    with pytest.raises(PermissionDenied):
        services.compute_snapshot(scored_event, "preview", actor=other)
    admin = make_user(role=ADMIN)
    assert services.compute_snapshot(scored_event, "final", actor=admin).created_by == admin
    assert refused_count() == 1


# --- 3. the config lock ----------------------------------------------------------------------------------

@TX
@pytest.mark.parametrize("override", [{"method": "zscore"}, {"config": {"primary": "raw_mean"}},
                                      {"weights": {"functionality": 2}}])
def test_final_refuses_any_override_even_after_judging(scored_event, at, override):
    at(scored_event.judging_ends_at + timedelta(days=1))
    with pytest.raises(FinalOverrideRefused) as caught:
        services.compute_snapshot(scored_event, "final", actor=scored_event.organizer, **override)
    assert (caught.value.status, caught.value.code) == (400, "final_uses_event_config")
    assert ResultSnapshot.objects.count() == 0


@TX
@pytest.mark.parametrize("flags", [["--method", "zscore"], ["--compare", "raw_mean"],
                                   ["--config", "duplicate_policy=merge"], ["--weights", "functionality=2"]])
def test_score_event_save_final_refuses_overrides(scored_event, at, flags):
    at(scored_event.judging_ends_at + timedelta(days=1))
    with pytest.raises(CommandError, match="final_uses_event_config"):
        call_command("score_event", scored_event.slug, "--save", "final", "--as", scored_event.organizer.email,
                     *flags, stdout=io.StringIO())
    assert ResultSnapshot.objects.count() == 0


@TX
def test_preview_accepts_overrides_in_every_phase(scored_event, at):
    for _, when in phases(scored_event):
        at(when)
        snapshot = services.compute_snapshot(
            scored_event, "preview", actor=scored_event.organizer, method="zscore",
            config={"compare": ["raw_mean"]}, weights={"functionality": 2},
        )
        assert snapshot.method == "zscore"
        assert [c["weight"] for c in snapshot.rubric["criteria"]] == [2.0, 1.0, 1.0]


@pytest.mark.django_db
def test_set_engine_config_before_close_and_locked_at_close(scored_event, at):
    at(scored_event.judging_ends_at - timedelta(seconds=1))
    row = services.set_engine_config(scored_event.organizer, scored_event, {"primary": "zscore"})
    assert row.overrides == {"primary": "zscore"}
    assert services.event_config(scored_event).primary == "zscore"
    assert AuditLog.objects.filter(action=AuditAction.SCORING_CONFIG_CHANGED).count() == 1
    for when in (scored_event.judging_ends_at, scored_event.judging_ends_at + timedelta(days=1)):
        at(when)
        with pytest.raises(ScoringConfigLocked) as caught:
            services.set_engine_config(scored_event.organizer, scored_event, {"primary": "m2"})
        assert (caught.value.status, caught.value.code) == (409, "scoring_config_locked")
    assert services.event_config(scored_event).primary == "zscore"
    assert refused_count(AuditAction.SCORING_CONFIG_REFUSED) == 2


@pytest.mark.django_db
def test_set_engine_config_validates_and_checks_permission(scored_event, at):
    at(scored_event.submissions_close_at)
    with pytest.raises(InvalidConfig) as caught:
        services.set_engine_config(scored_event.organizer, scored_event, {"tie_treshold": 0.9})
    assert caught.value.code == "invalid_config"
    with pytest.raises(PermissionDenied):
        services.set_engine_config(scored_event.judges[0].user, scored_event, {"primary": "m2"})


@TX
def test_final_uses_the_event_config(scored_event, at):
    at(scored_event.judging_ends_at - timedelta(days=1))
    services.set_engine_config(scored_event.organizer, scored_event, {"primary": "zscore", "compare": ["raw_mean"]})
    at(scored_event.judging_ends_at)
    snapshot = services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
    assert snapshot.method == "zscore"
    assert snapshot.engine_config["config"]["primary"] == "zscore"


# --- 4. immutability ---------------------------------------------------------------------------------------

def make_snapshot(event, user, kind="final"):
    return ResultSnapshot.objects.create(
        event=event, kind=kind, created_at=timezone.now(), created_by=user, created_by_email=user.email,
        method="m2", method_version="1", engine_config={}, rubric={}, input_hash="0" * 64,
        result={}, comparison={},
    )


@pytest.mark.django_db
def test_snapshot_cannot_be_saved_again(scored_event):
    snapshot = make_snapshot(scored_event, scored_event.organizer)
    snapshot.method = "zscore"
    with pytest.raises(SnapshotImmutable):
        snapshot.save()


@pytest.mark.django_db
def test_database_refuses_any_update_of_a_snapshot(scored_event):
    snapshot = make_snapshot(scored_event, scored_event.organizer)
    with pytest.raises(DatabaseError, match="dogfood_snapshot_immutable"), transaction.atomic():
        ResultSnapshot.objects.filter(pk=snapshot.pk).update(method="x")
    snapshot.refresh_from_db()
    assert snapshot.method == "m2"


@pytest.mark.django_db
def test_deleting_the_event_cascades_to_its_snapshots(scored_event):
    make_snapshot(scored_event, scored_event.organizer)
    with deadline_bypass(None, "test: delete event"):
        scored_event.delete()
    assert ResultSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_deleting_a_user_who_created_a_snapshot_is_protected(scored_event, make_user):
    creator = make_user(role=Role.ORGANIZER)
    make_snapshot(scored_event, creator)
    with pytest.raises(ProtectedError):
        creator.delete()


# --- 5. publication ---------------------------------------------------------------------------------------

def publish(event, snapshot, user):
    return Publication.objects.create(event=event, snapshot=snapshot, published_at=timezone.now(),
                                      published_by=user, published_by_email=user.email)


@pytest.mark.django_db
def test_publication_must_point_at_a_final_of_the_same_event(scored_event, make_event):
    organizer = scored_event.organizer
    preview = make_snapshot(scored_event, organizer, kind="preview")
    with pytest.raises(DatabaseError, match="dogfood_publication_not_final"), transaction.atomic():
        publish(scored_event, preview, organizer)
    other_event = make_event()
    other_final = make_snapshot(other_event, other_event.organizer)
    with pytest.raises(DatabaseError, match="dogfood_publication_wrong_event"), transaction.atomic():
        publish(scored_event, other_final, organizer)
    assert publish(scored_event, make_snapshot(scored_event, organizer), organizer).pk


@pytest.mark.django_db
def test_one_active_publication_per_event_and_append_only(scored_event, make_user):
    organizer = scored_event.organizer
    first = publish(scored_event, make_snapshot(scored_event, organizer), organizer)
    with pytest.raises(IntegrityError), transaction.atomic():
        publish(scored_event, make_snapshot(scored_event, organizer), organizer)

    with pytest.raises(DatabaseError, match="append_only"), transaction.atomic():
        Publication.objects.filter(pk=first.pk).update(published_at=timezone.now())
    with pytest.raises(DatabaseError, match="append_only"), transaction.atomic():
        Publication.objects.filter(pk=first.pk).update(snapshot=make_snapshot(scored_event, organizer))

    Publication.objects.filter(pk=first.pk).update(unpublished_at=timezone.now(), unpublished_by=organizer,
                                                   unpublished_by_email=organizer.email)
    with pytest.raises(DatabaseError, match="append_only"), transaction.atomic():
        Publication.objects.filter(pk=first.pk).update(unpublished_at=timezone.now())
    # once unpublished, another snapshot can be published
    assert publish(scored_event, make_snapshot(scored_event, organizer), organizer).pk


@pytest.mark.django_db
def test_published_snapshot_cannot_be_deleted_alone_but_the_event_can(scored_event):
    organizer = scored_event.organizer
    snapshot = make_snapshot(scored_event, organizer)
    publish(scored_event, snapshot, organizer)
    with pytest.raises(RestrictedError):
        snapshot.delete()
    with deadline_bypass(None, "test: delete event"):
        scored_event.delete()
    assert Publication.objects.count() == 0 and ResultSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_deleting_a_user_who_published_is_protected(scored_event, make_user):
    publisher = make_user(role=Role.ORGANIZER)
    publish(scored_event, make_snapshot(scored_event, scored_event.organizer), publisher)
    with pytest.raises(ProtectedError):
        publisher.delete()


# --- 6. the adapter on the fixture event ---------------------------------------------------------------------

def fixture_event():
    import_file(FIXTURES)
    ref = FixtureRef.objects.get(kind="event", external_id="evt_01")
    return Event.objects.get(pk=ref.object_id)


@pytest.mark.django_db
def test_build_input_on_the_fixture_event_under_both_policies():
    event = fixture_event()
    assert Score.objects.filter(project__event=event).count() == 123
    inp = services.build_input(event)
    assert len(inp.reviews) == 123                     # all of them; one marked as the duplicate's
    assert [r.project_id for r in inp.reviews].count("dup:prj_41") == 1
    for policy, candidates, used in (("exclude", 122, 119), ("merge", 123, 120)):
        result = pipeline.run(inp, "m2", {"duplicate_policy": policy})
        duplicate_drops = [e for e in result.excluded if e.kind == "review" and ":dup:" in e.id]
        assert len(inp.reviews) - len(duplicate_drops) == candidates, policy
        assert result.diagnostics["pipeline"]["reviews_used"] == used, policy
        assert "dup:prj_41" not in result.by_project()
    exclude = pipeline.run(inp, "m2")
    (dropped,) = [e for e in exclude.excluded if e.kind == "review" and ":dup:" in e.id]
    assert "duplicate submission dup:prj_41" in dropped.reason


@pytest.mark.django_db
def test_build_input_uses_the_events_weights():
    event = fixture_event()
    Criterion.objects.filter(event=event, key="functionality").update(weight=2.5)
    weights = {c.id: c.weight for c in services.build_input(event).rubric.criteria}
    # the importer's equal split, as percentages: 33.334 / 33.333 / 33.333 (functionality edited here)
    assert weights == {"functionality": 2.5, "quality": 33.333, "innovation": 33.333}
    overridden = {c.id: c.weight for c in services.build_input(event, weights={"quality": 3}).rubric.criteria}
    assert overridden == {"functionality": 2.5, "quality": 3.0, "innovation": 33.333}
    with pytest.raises(InvalidConfig):
        services.build_input(event, weights={"speed": 1})


@pytest.mark.django_db
def test_reviews_of_a_withdrawn_project_are_listed_not_dropped():
    event = fixture_event()
    project = Project.objects.filter(event=event).order_by("pk").first()
    reviews = Score.objects.filter(project=project).count()
    with deadline_bypass(None, "test: withdraw"):
        Project.objects.filter(pk=project.pk).update(status=Status.DRAFT, submitted_at=None)
    inp = services.build_input(event)
    assert str(project.pk) not in inp.projects
    listed = [e for e in inp.excluded if e.id.endswith(f":{project.pk}")]
    assert len(listed) == reviews > 0
    assert all((e.kind, e.reason) == ("review", "project not submitted") for e in listed)
    result = pipeline.run(inp, "m2")
    assert {e.id for e in listed} <= {e.id for e in result.excluded}


@pytest.mark.django_db
def test_only_submitted_reviews_count_and_drafts_are_listed(scored_event):
    draft = Score.objects.filter(project__event=scored_event).order_by("pk").first()
    Score.objects.filter(pk=draft.pk).update(submitted_at=None)
    empty = Score.objects.create(judge=scored_event.judges[0],
                                 project=Project.objects.create(team=make_team_for(scored_event), event=scored_event,
                                                                name="Empty", status=Status.SUBMITTED,
                                                                submitted_at=timezone.now()),
                                 submitted_at=timezone.now())
    inp = services.build_input(scored_event)
    assert len(inp.reviews) == 12 - 1
    reasons = {e.id: e.reason for e in inp.excluded}
    assert reasons[f"{draft.judge_id}:{draft.project_id}"] == "draft, not submitted"
    assert reasons[f"{empty.judge_id}:{empty.project_id}"] == "no scores"


def make_team_for(event):
    from teams.models import Team
    return Team.objects.create(event=event, name=f"Team {Team.objects.count() + 1}",
                               captain=event.organizer)


@TX
def test_judging_cannot_be_extended_once_a_final_exists(scored_event, at, monkeypatch):
    from events.models import Event
    from events.services import EventRuleError, extend_judging

    organizer = scored_event.organizer
    monkeypatch.setattr("core.deadlines.db_now", lambda: scored_event.judging_ends_at - timedelta(hours=1))
    services.compute_snapshot(scored_event, "preview", actor=organizer)  # previews don't block
    extend_judging(None, scored_event, scored_event.judging_ends_at + timedelta(days=1), "wifi")
    scored_event = Event.objects.get(pk=scored_event.pk)
    at(scored_event.judging_ends_at)
    services.compute_snapshot(scored_event, "final", actor=organizer)
    with pytest.raises(EventRuleError, match="final result"):
        extend_judging(None, scored_event, scored_event.judging_ends_at + timedelta(days=1), "again")
    assert AuditLog.objects.filter(action=AuditAction.JUDGING_EXTENSION_REFUSED).count() == 1


@pytest.mark.django_db
def test_database_result_equals_the_file_result():
    event = fixture_event()
    db = pipeline.compare(services.build_input(event), ["m2", "raw_mean", "zscore"], config={"cv_seed": LAB_SEED})
    # The same weights on both sides: the database stores the fixture's equal split as percentages
    # (33.334 / 33.333 / 33.333), which is not exactly equal thirds.
    weights = {c.key: float(c.weight) for c in Criterion.objects.filter(event=event)}
    file_input, _ = load_organizer_file(FIXTURES, weights=weights)
    fl = pipeline.compare(file_input, ["m2", "raw_mean", "zscore"], config={"cv_seed": LAB_SEED})
    project_id = {str(r.object_id): r.external_id
                  for r in FixtureRef.objects.filter(kind="project", duplicate_of="")}
    raw = json.loads(FIXTURES.read_text(encoding="utf-8"))
    judge_by_email = {j["email"].lower(): j["id"] for j in raw["judges"]}
    judge_id = {str(m.pk): judge_by_email[m.user.email.lower()]
                for m in EventMembership.objects.filter(event=event, role=Role.JUDGE).select_related("user")
                if m.user.email.lower() in judge_by_email}
    # Ranks follow R10: equal scores go by input order. The two inputs order projects
    # differently -- the file by its list, the database by creation, which the importer does
    # oldest submission first -- so inside a set of equal scores only the set of ranks must agree.
    for method in ("m2", "raw_mean", "zscore"):
        ours = {project_id[p.project_id]: p for p in db.results[method].projects}
        theirs = fl.results[method].by_project()
        assert set(ours) == set(theirs), method
        equal_sets = {}
        for p, row in ours.items():
            assert row.score == pytest.approx(theirs[p].score, abs=1e-12), (method, p)
            assert row.tie_group == theirs[p].tie_group, (method, p)
            equal_sets.setdefault(round(theirs[p].score, 9), []).append(p)
        for members in equal_sets.values():
            assert sorted(ours[p].rank for p in members) == sorted(theirs[p].rank for p in members), (method, members)
            if len(members) == 1:
                assert ours[members[0]].rank == theirs[members[0]].rank, (method, members)
    ours_m2, theirs_m2 = db.results["m2"], fl.results["m2"]
    assert ours_m2.params_chosen["components"][0]["lam_q"] == theirs_m2.params_chosen["components"][0]["lam_q"]
    assert ours_m2.params_chosen["components"][0]["lam_b"] == theirs_m2.params_chosen["components"][0]["lam_b"]
    theirs_bias = {j.judge_id: j.bias for j in theirs_m2.judges}
    for j in ours_m2.judges:
        assert j.bias == pytest.approx(theirs_bias[judge_id[j.judge_id]], abs=1e-12)


@pytest.mark.django_db
def test_input_hash_is_stable_and_follows_the_scores():
    event = fixture_event()
    first = services.input_hash(services.build_input(event))
    assert services.input_hash(services.build_input(event)) == first
    item = ScoreItem.objects.filter(score__project__event=event).order_by("pk").first()
    ScoreItem.objects.filter(pk=item.pk).update(value=5 if item.value != 5 else 4)
    assert services.input_hash(services.build_input(event)) != first


# --- 6b. the transaction ----------------------------------------------------------------------------------------

@TX
def test_snapshot_reads_at_repeatable_read(scored_event, at, monkeypatch):
    at(scored_event.judging_ends_at)
    seen = []
    real = services.build_input

    def spy(event, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            seen.append(cursor.fetchone()[0])
        return real(event, **kwargs)

    monkeypatch.setattr(services, "build_input", spy)
    services.compute_snapshot(scored_event, "preview", actor=scored_event.organizer)
    assert seen == ["repeatable read"]


@TX
def test_snapshot_refuses_to_run_inside_another_transaction(scored_event, at):
    at(scored_event.judging_ends_at)
    with transaction.atomic():
        with pytest.raises(SnapshotInsideTransaction):
            services.compute_snapshot(scored_event, "preview", actor=scored_event.organizer)
    assert ResultSnapshot.objects.count() == 0


# --- 6c. several finals ---------------------------------------------------------------------------------------------

@TX
def test_each_final_records_the_previous_one(scored_event, at):
    at(scored_event.judging_ends_at)   # the same instant for all three: (created_at, id) orders them
    first = services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
    assert first.diagnostics == {"previous_final": None, "scores_changed_since_last_final": False}

    services.compute_snapshot(scored_event, "preview", actor=scored_event.organizer)  # previews don't count
    second = services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
    assert second.diagnostics["previous_final"]["id"] == first.pk
    assert second.diagnostics["previous_final"]["input_hash"] == first.input_hash
    assert second.diagnostics["scores_changed_since_last_final"] is False

    item = ScoreItem.objects.filter(score__project__event=scored_event).order_by("pk").first()
    ScoreItem.objects.filter(pk=item.pk).update(value=5 if item.value != 5 else 4)
    third = services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
    assert third.diagnostics["previous_final"]["id"] == second.pk
    assert third.diagnostics["scores_changed_since_last_final"] is True
    assert third.input_hash != second.input_hash


# --- 7-10. what a snapshot stores; the commands ---------------------------------------------------------------------

@TX
def test_snapshot_stores_the_resolved_config_and_rubric(scored_event, at):
    at(scored_event.judging_ends_at)
    snapshot = services.compute_snapshot(scored_event, "final", actor=scored_event.organizer)
    config = snapshot.engine_config
    assert config["config"]["cv_seed"] == seed_for(scored_event.slug)
    assert isinstance(config["config"]["cv_seed"], int)
    assert config["components"] and all(
        isinstance(c["lam_q"], float) and isinstance(c["lam_b"], float) for c in config["components"])
    assert snapshot.rubric["criteria"][0] == {"id": "functionality", "weight": 1.0, "min": 1.0, "max": 5.0}
    assert snapshot.created_by_email == scored_event.organizer.email
    assert snapshot.result["method"] == "m2" and snapshot.comparison["primary"] == "m2"
    assert snapshot.input_hash == services.input_hash(services.build_input(scored_event))


@pytest.mark.django_db
def test_score_methods_lists_the_registry():
    out = io.StringIO()
    call_command("score_methods", stdout=out)
    lines = out.getvalue().splitlines()
    assert any(line.startswith("m2") and "v1" in line and "uncertainty" in line for line in lines)
    assert any(line.startswith("raw_mean") for line in lines) and any(line.startswith("zscore") for line in lines)


@pytest.mark.django_db
def test_score_event_without_save_writes_nothing(scored_event):
    before = (ResultSnapshot.objects.count(), AuditLog.objects.count())
    out = io.StringIO()
    call_command("score_event", scored_event.slug, "--compare", "raw_mean", stdout=out)
    assert "method m2 v1 | baseline raw_mean" in out.getvalue()
    assert "raw_rank" in out.getvalue() and "not saved" in out.getvalue()
    assert (ResultSnapshot.objects.count(), AuditLog.objects.count()) == before


@pytest.mark.django_db
def test_score_event_names_projects_never_bare_ids():
    """Fixture projects show as "name (fixture id)", the folded duplicate as "..., duplicate
    (prj_41)", judges by email, tracks by name; no bare database id anywhere in the table."""
    event = fixture_event()
    out = io.StringIO()
    call_command("score_event", event.slug, stdout=out)
    text = out.getvalue()
    kept = Project.objects.get(pk=FixtureRef.objects.get(kind="project", external_id="prj_07").object_id)
    assert f"{kept.name} (prj_07)" in text
    assert f"{kept.name}, duplicate (prj_41)" in text
    table = [line for line in text.splitlines() if line[:4].strip().isdigit()]
    assert len(table) == 40
    assert all("(prj_" in line for line in table)
    track = event.tracks.first()
    assert track.name in text
    assert "@" in text.split("# excluded judge ")[1].split(":")[0]   # the flat judge, by email
    excluded = [line for line in text.splitlines() if line.startswith("# excluded")]
    assert excluded and all(not re.search(r"(kept: |submission of |flat judge )\d", line) for line in excluded)
    assert f"(kept: {kept.name} (prj_07))" in text


@pytest.mark.django_db
def test_project_without_a_fixture_id_is_named_with_its_pk(scored_event):
    out = io.StringIO()
    call_command("score_event", scored_event.slug, stdout=out)
    project = scored_event.projects_list[0]
    assert f"{project.name} (#{project.pk})" in out.getvalue()


@TX
def test_score_event_saves_a_final_as_an_organizer(scored_event, at):
    at(scored_event.judging_ends_at)
    out = io.StringIO()
    call_command("score_event", scored_event.slug, "--save", "final", "--as", scored_event.organizer.email, stdout=out)
    assert "saved final snapshot" in out.getvalue()
    with pytest.raises(CommandError, match="--as"):
        call_command("score_event", scored_event.slug, "--save", "preview", stdout=io.StringIO())
