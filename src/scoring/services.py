"""Scoring services: the only code that turns the database into engine input and stores results.

* `build_input(event)`     the event's submitted projects, reviews and rubric, as an EngineInput.
* `judging_closed(event, now)`  THE one place that decides whether judging is over. Nothing else
                           may compare against `judging_ends_at` (judging extensions will plug in
                           here later).
* `compute_snapshot(...)`  a preview (any time) or a final (only after judging closed) result.
* `set_engine_config(...)` an event's engine configuration; locked once judging has closed.

Order of checks, everywhere: permission first (403), then the refusal of overrides on a final,
then the window (409), then validation. A judge or participant is refused as unauthorised in
every phase, never told whether judging is open.

`compute_snapshot` opens its own transaction at REPEATABLE READ, so the reviews it reads, the
previous final it compares with and the row it writes all see one consistent database state. It
must not be called inside another transaction (it raises): ATOMIC_REQUESTS is off in this project,
and any future view that calls it must be decorated @transaction.non_atomic_requests.
"""

from __future__ import annotations

import hashlib

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from accounts.roles import is_organizer_of
from core import audit
from core.deadlines import db_now
from core.models import AuditAction
from imports.models import FixtureRef
from projects.models import Project, Status

from .engine import pipeline
from .engine.config import EngineConfig
from .engine.errors import ConfigError, EngineError
from .engine.types import Criterion, EngineInput, Exclusion, Review, Rubric, dumps, to_jsonable
from .errors import (FinalOverrideRefused, InvalidConfig, JudgingOpen, ScoringConfigLocked,
                     SnapshotInsideTransaction)
from .models import Criterion as CriterionRow
from .models import EventScoringConfig, ResultSnapshot, Score, SnapshotKind

FIXTURE_SOURCE = "dogfood-fixtures"


# --- the window -----------------------------------------------------------------------------------

def judging_closed(event, now) -> bool:
    """Whether judging for `event` is over at `now` (the database clock). The boundary instant
    counts as closed, like the submission window."""
    return now >= event.judging_ends_at


# --- engine input ----------------------------------------------------------------------------------

def build_input(event, *, weights: dict | None = None) -> EngineInput:
    """The event as the engine sees it.

    * projects: the event's *submitted* projects, by id, with their track.
    * reviews: every Score of the event, in creation order (the order the engine's
      cross-validation folds depend on). A review of a project that is not submitted is listed in
      `excluded` ("project not submitted"), never silently dropped.
    * duplicates: submissions the fixture importer folded into another. A review the importer
      moved from a duplicate onto the kept project is given back to the duplicate here (under the
      id "dup:<fixture id>"), so the engine's duplicate policy decides what happens to it -- the
      same as when it reads the fixture file.
    * rubric: the event's criteria and weights (`weights`, a {key: weight} override, is for
      previews only; `compute_snapshot` refuses it on a final).
    * event_id: the slug, from which the default CV seed is derived.
    """
    criteria = list(CriterionRow.objects.filter(event=event).order_by("order", "key"))
    weights = weights or {}
    unknown = sorted(set(weights) - {c.key for c in criteria})
    if unknown:
        raise InvalidConfig(f"Weights name criteria this event does not have: {', '.join(unknown)}.")
    rubric = Rubric(tuple(
        Criterion(c.key, float(weights.get(c.key, c.weight)), float(c.min_value), float(c.max_value))
        for c in criteria
    ))

    submitted = Project.objects.filter(event=event, status=Status.SUBMITTED).order_by("pk")
    projects = {str(p.pk): (str(p.track_id) if p.track_id else None) for p in submitted}

    # Duplicates folded in by the importer: project ref with duplicate_of -> the kept project row.
    duplicate_of_pk = {}   # fixture id of the duplicate -> kept project's pk
    for ref in FixtureRef.objects.filter(source=FIXTURE_SOURCE, kind=FixtureRef.Kind.PROJECT).exclude(duplicate_of=""):
        if str(ref.object_id) in projects:
            duplicate_of_pk[ref.external_id] = str(ref.object_id)
    duplicates = {}
    for external_id, kept in duplicate_of_pk.items():
        dup_id = f"dup:{external_id}"
        projects[dup_id] = projects[kept]
        duplicates[dup_id] = kept

    scores = list(
        Score.objects.filter(project__event=event).order_by("pk").prefetch_related("items__criterion")
    )
    moved = {}  # score pk -> duplicate engine id, for reviews the importer took off a duplicate
    if duplicate_of_pk:
        refs = FixtureRef.objects.filter(source=FIXTURE_SOURCE, kind=FixtureRef.Kind.SCORE,
                                         object_id__in=[s.pk for s in scores])
        for ref in refs:
            project_external = ref.external_id.rsplit(":", 1)[-1]
            if project_external in duplicate_of_pk:
                moved[ref.object_id] = f"dup:{project_external}"

    reviews, excluded = [], []
    for score in scores:
        project_id = moved.get(score.pk, str(score.project_id))
        judge_id = str(score.judge_id)
        if project_id not in projects:
            excluded.append(Exclusion("review", f"{judge_id}:{project_id}", "project not submitted"))
            continue
        items = {item.criterion.key: float(item.value) for item in score.items.all()}
        reviews.append(Review(judge_id, project_id, items, projects[project_id]))
    return EngineInput(event_id=event.slug, reviews=tuple(reviews), rubric=rubric, projects=projects,
                       duplicates=duplicates, excluded=tuple(excluded))


def input_hash(inp: EngineInput) -> str:
    return hashlib.sha256(dumps(inp).encode("utf-8")).hexdigest()


def event_config(event) -> EngineConfig:
    row = EventScoringConfig.objects.filter(event=event).first()
    return EngineConfig.from_dict(row.overrides if row else {})


# --- snapshots ------------------------------------------------------------------------------------

def compute_snapshot(event, kind, *, actor, method=None, config=None, weights=None, request=None):
    """Compute and store a result snapshot. Returns the ResultSnapshot, with the engine's
    ComparisonResult attached as `.comparison_result` (not stored as an object; the JSON is).

    Refusals, in this order: PermissionDenied (not an organizer of the event, nor an admin);
    FinalOverrideRefused (a final with a method, config or weights override); JudgingOpen (a final
    before judging closed); InvalidConfig. Each is audited, outside any transaction.
    """
    if kind not in SnapshotKind.values:
        raise ValueError(f"kind must be one of {SnapshotKind.values}")

    def refuse(error, reason):
        audit.record(AuditAction.SNAPSHOT_REFUSED, request=request, actor=actor, subject=event.slug,
                     kind=kind, reason=reason)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can compute results."), "not an organizer")
    if kind == SnapshotKind.FINAL and (method or config or weights):
        overrides = [name for name, value in (("method", method), ("config", config), ("weights", weights)) if value]
        refuse(FinalOverrideRefused(
            "A final result always uses the event's own scoring configuration and weights; "
            f"remove {', '.join(overrides)}, or compute a preview."), f"override: {', '.join(overrides)}")
    if kind == SnapshotKind.FINAL:
        now = db_now()
        if not judging_closed(event, now):
            refuse(JudgingOpen(f"Judging for {event.slug} closes at {event.judging_ends_at.isoformat()}; "
                               "a final result can only be computed after that. A preview can be computed now."),
                   "judging open")
    if connection.in_atomic_block:
        raise SnapshotInsideTransaction(
            "compute_snapshot opens its own REPEATABLE READ transaction and cannot run inside another. "
            "Call it outside transaction.atomic (views: @transaction.non_atomic_requests)."
        )

    try:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                # Must be the first statement of the transaction.
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cfg = event_config(event)
            if kind == SnapshotKind.PREVIEW and config:
                cfg = cfg.with_overrides(config)
            primary = method or cfg.primary
            inp = build_input(event, weights=weights if kind == SnapshotKind.PREVIEW else None)
            comparison = pipeline.compare(inp, [primary, *cfg.compare], config=cfg)
            result = comparison.results[primary]
            digest = input_hash(inp)
            diagnostics = {}
            if kind == SnapshotKind.FINAL:
                previous = (ResultSnapshot.objects.filter(event=event, kind=SnapshotKind.FINAL)
                            .order_by("-created_at", "-id").first())
                diagnostics["previous_final"] = None if previous is None else {
                    "id": previous.pk, "created_at": previous.created_at.isoformat(),
                    "input_hash": previous.input_hash,
                }
                diagnostics["scores_changed_since_last_final"] = (
                    previous is not None and previous.input_hash != digest
                )
            snapshot = ResultSnapshot.objects.create(
                event=event, kind=kind, created_at=db_now(), created_by=actor,
                created_by_email=actor.email, method=result.method, method_version=result.method_version,
                engine_config=_resolved_config(result), rubric=to_jsonable(inp.rubric),
                input_hash=digest, result=to_jsonable(result), comparison=to_jsonable(comparison),
                diagnostics=diagnostics,
            )
    except InvalidConfig as error:
        refuse(error, f"invalid: {error.detail}")
    except (ConfigError, EngineError) as error:
        refuse(InvalidConfig(str(error)), f"invalid: {error}")
    audit.record(AuditAction.SNAPSHOT_CREATED, request=request, actor=actor, subject=event.slug,
                 kind=kind, snapshot=snapshot.pk, method=snapshot.method, input_hash=digest)
    snapshot.comparison_result = comparison
    return snapshot


def _resolved_config(result) -> dict:
    """The configuration exactly as used: every key, the seed resolved, the lambdas per component."""
    return {
        "config": dict(result.config),
        "components": [
            {key: value for key, value in params.items()
             if key in ("component", "lam_q", "lam_b", "lambda_source", "cv_seed", "lambda_at_grid_boundary")}
            for params in result.params_chosen["components"]
        ],
    }


# --- configuration -----------------------------------------------------------------------------------

def set_engine_config(actor, event, overrides, *, request=None) -> EventScoringConfig:
    """Replace the event's engine configuration overrides. Refused once judging has closed."""

    def refuse(error, reason):
        audit.record(AuditAction.SCORING_CONFIG_REFUSED, request=request, actor=actor, subject=event.slug,
                     reason=reason)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can change its scoring configuration."),
               "not an organizer")
    if judging_closed(event, db_now()):
        refuse(ScoringConfigLocked("Judging has closed: the scoring configuration is locked, because a "
                                   "final result must use the configuration judging ran under."), "locked")
    try:
        EngineConfig.from_dict(overrides)
    except ConfigError as error:
        refuse(InvalidConfig(str(error)), f"invalid: {error}")
    row, _ = EventScoringConfig.objects.update_or_create(
        event=event, defaults={"overrides": overrides, "updated_by": actor})
    audit.record(AuditAction.SCORING_CONFIG_CHANGED, request=request, actor=actor, subject=event.slug,
                 overrides=overrides)
    return row
