"""Every write to an event's rubric and its assignments. Pages and any API call these,
so they cannot enforce different rules. Each write leaves an audit row.

The rubric rules
----------------
* **Weights are relative**: each is a number above 0, and a criterion's share of the score is
  its weight divided by the sum (1 / 1 / 1 is three equal thirds, exactly; 2 / 1 / 1 is half and
  two quarters). Pages show the share as a percentage (`weight_shares`); the scoring engine
  normalises the same way. Nothing has to add up to 100, so equal weights stay exactly equal --
  a rounded 33.334 / 33.333 / 33.333 would quietly favour the first criterion. The whole rubric
  is saved in one go (`save_rubric`).
* **Every criterion is scored 1-5** (`SCALE_MIN`/`SCALE_MAX`, and a CHECK constraint): the scoring
  engine's constants are tuned for that scale. Organizers may describe any of the five levels and
  leave the rest empty.
* **Locked from the submission close.** From `submissions_close_at` on (by the database clock,
  like the deadline), anything that changes the ranking is refused: weights, the scale's min and
  max, and adding or removing criteria. An extension for everyone moves the close, and the lock
  with it. Labels, descriptions and the written anchor per score level stay editable
  (`update_criterion_text`), because fixing a typo mid-judging changes nobody's result; every
  such edit is still audited.
* **Nothing already scored is rewritten.** A criterion with scores cannot be removed, and its
  scale cannot shrink past a value already given.

The results rules
-----------------
Scoring services: the only code that turns the database into engine input and stores results.

* `build_input(event)`     the event's submitted projects, reviews and rubric, as an EngineInput.
* `judging_closed(event, now)`  THE rule for whether judging is over; defined in core.judging and
                           shared with the judges' write window and the assignment freeze.
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
import math
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone
from django.utils.text import slugify

from accounts.roles import Role, is_organizer_of
from core import audit
from core.deadlines import db_now
from core.judging import judging_closed
from core.models import AuditAction
from events.models import Event, EventMembership
from imports.models import FixtureRef
from projects.models import Project, Status
from scoring.models import (
    WINNERS_TOP_N_MAX, Assignment, AssignmentRound, AssignmentSource, AssignmentStatus, Criterion,
    EventResultSettings, EventScoringConfig, Publication, ResultSnapshot, ResultVisibility, RoundKind,
    Score, ScoreItem, SnapshotKind,
)

from voting import services as voting_services
from voting.errors import VotingOpen
from voting.models import VotingConfig

from .engine import pipeline
from .engine.config import EngineConfig
from .engine.errors import ConfigError, EngineError
from .engine.types import Criterion as EngineCriterion
from .engine.types import EngineInput, Exclusion, Review, Rubric, dumps, to_jsonable
from .engine import combine as combining
from .errors import (AlreadyPublished, ConcurrentFinal, FinalOverrideRefused, FinalPredatesVoteClose, InvalidConfig, InvalidResultSettings,
                     InvalidWeights, JudgingOpen, NoSuchSnapshot, NotFinal, NotLatestFinal, NotPublished,
                     NoVoteForCommunityWeight, ScoringConfigLocked, SnapshotInsideTransaction, WeightsLocked)

FIXTURE_SOURCE = "dogfood-fixtures"  # imports.fixtures.SOURCE

WEIGHT_STEP = Decimal("0.001")  # Criterion.weight has three decimal places
WEIGHT_MAX = Decimal("999.999")  # Criterion.weight: max_digits=6, decimal_places=3
SCALE_MIN, SCALE_MAX = 1, 5  # every criterion; the engine is tuned for this scale


class RubricError(Exception):
    """A refused rubric change, with a sentence the page can show as-is."""


class RubricLocked(RubricError):
    pass


# --- the lock ---------------------------------------------------------------------------------


def rubric_locked(event, now=None):
    """True from the submission close onward: judging can start, so the ranking rules are fixed."""
    return (now or db_now()) >= event.submissions_close_at


def _refuse_if_locked(request, event, what):
    if rubric_locked(event):
        audit.record(
            AuditAction.RUBRIC_CHANGE_REFUSED, request=request, subject=event.slug, change=what,
            locked_since=event.submissions_close_at.isoformat(),
        )
        raise RubricLocked(
            "the rubric is locked: submissions closed, so weights and the set of "
            "criteria can no longer change. labels and level descriptions can still be edited."
        )


# --- weights ----------------------------------------------------------------------------------


def split_equally(n):
    """`n` equal relative weights: 1 each (each criterion then counts exactly 1/n)."""
    return [Decimal(1)] * max(n, 0)


def weight_shares(criteria):
    """{criterion pk: its share of the score as a percentage, one decimal} -- for display. The
    stored weights are relative; this is weight / sum of the event's weights."""
    criteria = list(criteria)
    total = sum((Decimal(c.weight) for c in criteria), Decimal(0))
    if total <= 0:
        return {c.pk: None for c in criteria}
    return {c.pk: (Decimal(c.weight) * 100 / total).quantize(Decimal("0.1")) for c in criteria}


def with_shares(criteria):
    """The criteria as a list, each with `.share` (percentage) set, for templates."""
    criteria = list(criteria)
    shares = weight_shares(criteria)
    for c in criteria:
        c.share = shares[c.pk]
    return criteria


# --- the standard rubric -----------------------------------------------------------------------

# The organizers' fixture scores these three criteria on 1-5; offering them as a starting point
# means a new event is one click from a rubric that matches the shared data.
STANDARD_RUBRIC = [
    ("functionality", "Functionality", "Does it work, end to end, for the problem it claims to solve?", {
        "1": "Does not run, or the core flow is missing.",
        "2": "Runs, but the core flow breaks or is mostly faked.",
        "3": "The core flow works with rough edges or manual steps.",
        "4": "Works end to end; minor gaps only.",
        "5": "Works end to end, handles errors and edge cases, ready for real users.",
    }),
    ("quality", "Quality", "Code, design and documentation a stranger could pick up.", {
        "1": "Hard to read or run; no docs.",
        "2": "Runs with effort; thin docs, inconsistent structure.",
        "3": "Readable and runnable; some tests or docs.",
        "4": "Clean structure, sensible tests, clear docs.",
        "5": "Idiomatic, well tested, documented well enough to adopt.",
    }),
    ("innovation", "Innovation", "Is the idea or the approach new, or notably better than what exists?", {
        "1": "A copy of something that exists, with nothing added.",
        "2": "A familiar idea with a small twist.",
        "3": "A sensible new angle on a known problem.",
        "4": "A clearly new approach, well chosen.",
        "5": "Something the judges would steal.",
    }),
]


def create_standard_rubric(event):
    """The standard three criteria, split equally. No lock check and no audit row: callers are
    `use_standard_rubric` (which does both) and the demo seed (which runs at boot)."""
    weights = split_equally(len(STANDARD_RUBRIC))
    return [
        Criterion.objects.create(
            event=event, key=key, label=label, description=description,
            level_descriptions=levels, weight=weight, order=order,
        )
        for order, ((key, label, description, levels), weight) in enumerate(
            zip(STANDARD_RUBRIC, weights), start=1
        )
    ]


def use_standard_rubric(request, event):
    """Give an event with no criteria the standard three, split equally. Refused once locked."""
    _refuse_if_locked(request, event, "standard rubric")
    with transaction.atomic():
        Event.objects.select_for_update().get(pk=event.pk)
        if event.criteria.exists():
            raise RubricError("this event already has a rubric; edit it instead.")
        criteria = create_standard_rubric(event)
    for criterion in criteria:
        audit.record(
            AuditAction.CRITERION_ADDED, request=request, subject=event.slug,
            key=criterion.key, weight=str(criterion.weight),
            scale=[criterion.min_value, criterion.max_value], source="standard rubric",
        )


# --- saving the whole rubric ------------------------------------------------------------------


@dataclass
class RubricRow:
    """One line of the rubric as the organizer submitted it. `id` is None for a new criterion."""

    id: int | None
    key: str
    label: str
    weight: Decimal
    min_value: int = SCALE_MIN
    max_value: int = SCALE_MAX
    order: int = 1
    delete: bool = False


RANKING_FIELDS = ("weight", "min_value", "max_value")


def validate_rows(rows):
    """The rules a rubric must satisfy, whoever submits it. Returns the rows that are kept."""
    kept = [r for r in rows if not r.delete]
    if not kept:
        raise RubricError("a rubric needs at least one criterion.")
    keys = set()
    for row in kept:
        row.label = (row.label or "").strip()
        if not row.label:
            raise RubricError("every criterion needs a label.")
        row.key = slugify(row.key or row.label)[:60]
        if not row.key:
            raise RubricError(f"'{row.label}' needs a key made of letters or digits.")
        if row.key in keys:
            raise RubricError(f"two criteria share the key '{row.key}'; keys must differ.")
        keys.add(row.key)
        if row.weight is None or row.weight <= 0:
            raise RubricError(f"'{row.label}': a weight must be a number above 0.")
        if row.weight > WEIGHT_MAX:
            raise RubricError(f"'{row.label}': a weight can be at most {WEIGHT_MAX}.")
        if row.weight != row.weight.quantize(WEIGHT_STEP):
            raise RubricError(f"'{row.label}': weights have at most three decimal places.")
        row.weight = row.weight.quantize(WEIGHT_STEP)  # as stored: 60 -> 60.000
        if (row.min_value, row.max_value) != (SCALE_MIN, SCALE_MAX):
            raise RubricError(
                f"'{row.label}': every criterion is scored on the scale {SCALE_MIN}-{SCALE_MAX}."
            )
    return kept


def save_rubric(request, event, rows):
    """Replace the event's rubric with `rows`, all or nothing, and audit every change."""
    _refuse_if_locked(request, event, "rubric")
    kept = validate_rows(rows)
    with transaction.atomic():
        # One rubric edit at a time per event: two organizers saving at once cannot interleave
        # into a rubric that adds up to 130.
        Event.objects.select_for_update().get(pk=event.pk)
        existing = {c.pk: c for c in event.criteria.all()}
        unknown = [r.id for r in rows if r.id is not None and r.id not in existing]
        if unknown:
            raise RubricError("the rubric changed while you were editing it; reload and try again.")

        changes = []
        for row in rows:
            if row.delete and row.id is not None:
                criterion = existing[row.id]
                if ScoreItem.objects.filter(criterion=criterion).exists():
                    raise RubricError(f"'{criterion.label}' already has scores, so it cannot be removed.")
                changes.append((AuditAction.CRITERION_REMOVED, {"key": criterion.key}))
                criterion.delete()

        # Keys are unique per event; free the old keys first so two criteria can swap keys.
        for row in kept:
            if row.id is not None and existing[row.id].key != row.key:
                Criterion.objects.filter(pk=row.id).update(key=f"__renaming-{row.id}")

        for row in kept:
            if row.id is None:
                criterion = Criterion.objects.create(
                    event=event, key=row.key, label=row.label, weight=row.weight,
                    min_value=row.min_value, max_value=row.max_value, order=row.order,
                )
                changes.append((AuditAction.CRITERION_ADDED, {
                    "key": row.key, "weight": str(row.weight),
                    "scale": [row.min_value, row.max_value],
                }))
                continue
            criterion = existing[row.id]
            before = {f: getattr(criterion, f) for f in ("key", "label", "weight", "min_value", "max_value", "order")}
            if (row.min_value, row.max_value) != (criterion.min_value, criterion.max_value):
                given = ScoreItem.objects.filter(criterion=criterion)
                if given.filter(value__lt=row.min_value).exists() or given.filter(value__gt=row.max_value).exists():
                    raise RubricError(
                        f"'{criterion.label}' has scores outside {row.min_value}-{row.max_value}; "
                        "the scale cannot shrink past them."
                    )
            criterion.key, criterion.label, criterion.weight = row.key, row.label, row.weight
            criterion.min_value, criterion.max_value, criterion.order = row.min_value, row.max_value, row.order
            criterion.level_descriptions = {
                k: v for k, v in criterion.level_descriptions.items()
                if k.lstrip("-").isdigit() and row.min_value <= int(k) <= row.max_value
            }
            criterion.save()
            after = {f: getattr(criterion, f) for f in before}
            diff = {f: [str(before[f]), str(after[f])] for f in before if before[f] != after[f]}
            if diff:
                changes.append((AuditAction.CRITERION_UPDATED, {"key": criterion.key, "changed": diff}))

    for action, detail in changes:
        audit.record(action, request=request, subject=event.slug, **detail)
    return changes


# --- text that may change at any time ------------------------------------------------------------


def update_criterion_text(request, criterion, *, label, description, level_descriptions):
    """Edit what judges read -- the label, the description and the anchor per score level.

    Allowed even while the rubric is locked: wording does not change anyone's ranking. The key,
    weight and scale are not touched here.
    """
    label = (label or "").strip()
    if not label:
        raise RubricError("a criterion needs a label.")
    levels = {}
    for level, text in (level_descriptions or {}).items():
        text = (text or "").strip()
        if not text:
            continue
        if not (str(level).lstrip("-").isdigit() and criterion.min_value <= int(level) <= criterion.max_value):
            raise RubricError(f"{level} is not a level on this criterion's scale.")
        levels[str(int(level))] = text[:300]
    before = {"label": criterion.label, "description": criterion.description, "levels": criterion.level_descriptions}
    criterion.label, criterion.description = label, (description or "").strip()
    criterion.level_descriptions = levels
    criterion.save(update_fields=["label", "description", "level_descriptions", "updated_at"])
    after = {"label": criterion.label, "description": criterion.description, "levels": criterion.level_descriptions}
    changed = [f for f in before if before[f] != after[f]]
    if changed:
        audit.record(
            AuditAction.CRITERION_UPDATED, request=request, subject=criterion.event.slug,
            key=criterion.key, changed=changed,
            while_locked=rubric_locked(criterion.event),
        )
    return changed


# --- assignment -----------------------------------------------------------------------------------
#
# Who reviews what. The plan comes from scoring.assignment (hard rules, balance, seeded
# randomness, connectivity); these functions check the timing, write the rows and audit them.
# Assignments are never deleted: withdrawing or declining changes the status.

DEFAULT_TARGET = 3


class AssignmentError(Exception):
    """A refused assignment change, with a sentence the page can show as-is."""


def review_target(event):
    """The review target of the event's latest round, or the default of 3."""
    latest = event.assignment_rounds.order_by("-created_at", "-id").first()
    return latest.target_reviews if latest else DEFAULT_TARGET


def _refuse_if_judging_over(event):
    if judging_closed(event, db_now()):
        raise AssignmentError("judging has ended, so assignments are frozen. extend judging first.")


def run_assignment(request, event, *, target=DEFAULT_TARGET, max_load=None, seed=None,
                   exclude_judges=(), kind=None):
    """Assign judges until every submitted project has `target` reviews (where the rules allow).

    Returns the AssignmentRound, whose `summary` says what was added, what is still short, and
    any warnings. `seed` reproduces an earlier round exactly (given the same starting state).
    """
    from scoring.assignment import make_plan

    _refuse_if_judging_over(event)
    if not 1 <= target <= 20:
        raise AssignmentError("the review target must be between 1 and 20.")
    if max_load is not None and max_load < 1:
        raise AssignmentError("the load cap must be at least 1, or empty for no cap.")
    seed = seed if seed is not None else secrets.randbits(62)
    with transaction.atomic():
        Event.objects.select_for_update().get(pk=event.pk)
        plan = make_plan(event, target=target, max_load=max_load, seed=seed, exclude_judges=exclude_judges)
        if kind is None:
            kind = RoundKind.TOP_UP if Assignment.objects.filter(project__event=event).exists() else RoundKind.INITIAL
        warnings = list(plan.warnings)
        if db_now() < event.submissions_close_at:
            warnings.insert(0, "submissions are still open: projects submitted later will need a top-up.")
        round_ = AssignmentRound.objects.create(
            event=event, kind=kind, seed=seed, target_reviews=target, max_load=max_load,
            created_by=request.user if request and request.user.is_authenticated else None,
            summary={
                "added": len(plan.new),
                "bridges": len(plan.bridges),
                "short": {str(p): n for p, n in sorted(plan.short.items())},
                "warnings": warnings,
                "islands_before": plan.islands_before,
                "islands_after": plan.islands_after,
                "excluded_judges": sorted(getattr(j, "pk", j) for j in exclude_judges),
            },
        )
        now = timezone.now()
        Assignment.objects.bulk_create([
            Assignment(
                judge_id=j, project_id=p, round=round_, source=AssignmentSource.AUTO,
                position=plan.positions[(j, p)], created_by=round_.created_by, created_at=now,
            )
            for j, p in plan.new
        ])
    audit.record(
        AuditAction.ASSIGNMENTS_GENERATED, request=request, subject=event.slug, round=round_.pk,
        kind=kind, seed=seed, target=target, max_load=max_load, added=len(plan.new),
        short=sum(plan.short.values()), warnings=len(warnings),
    )
    return round_


def _check_can_review(event, judge, project):
    from accounts.roles import Role

    if judge.event_id != event.pk or judge.role != Role.JUDGE:
        raise AssignmentError("that account is not a judge of this event.")
    if project.event_id != event.pk:
        raise AssignmentError("that project is not in this event.")
    from projects.models import Status

    if project.status != Status.SUBMITTED:
        raise AssignmentError("only submitted projects are judged.")
    tracks = set(judge.judge_tracks.values_list("track_id", flat=True))
    if tracks and project.track_id is not None and project.track_id not in tracks:
        raise AssignmentError(f"{judge.user.email} does not judge the {project.track.name} track.")
    live = Assignment.objects.filter(judge=judge, project=project, status__in=[
        AssignmentStatus.ASSIGNED, AssignmentStatus.DECLINED])
    if live.filter(status=AssignmentStatus.DECLINED).exists():
        raise AssignmentError(f"{judge.user.email} declined this project (conflict of interest).")
    if live.exists():
        raise AssignmentError(f"{judge.user.email} is already assigned {project.name}.")


def _next_position(judge):
    last = Assignment.objects.filter(judge=judge).order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else last + 1


def add_assignment(request, event, judge, project):
    """Assign one project to one judge by hand, under the same rules as the automatic rounds."""
    _refuse_if_judging_over(event)
    _check_can_review(event, judge, project)
    try:
        with transaction.atomic():
            assignment = Assignment.objects.create(
                judge=judge, project=project, source=AssignmentSource.MANUAL,
                position=_next_position(judge), created_by=request.user,
            )
    except IntegrityError as error:
        raise AssignmentError("that judge was just assigned this project.") from error
    audit.record(
        AuditAction.ASSIGNMENT_ADDED, request=request, subject=event.slug,
        judge=judge.user.email, project=project.pk,
    )
    return assignment


def _has_review(assignment):
    return Score.objects.filter(judge=assignment.judge_id, project=assignment.project_id).exists()


def withdraw_assignment(request, event, assignment, *, action=AuditAction.ASSIGNMENT_WITHDRAWN, **detail):
    """Take a project back from a judge who has not started reviewing it."""
    _refuse_if_judging_over(event)
    if assignment.project.event_id != event.pk:
        raise AssignmentError("that assignment is not in this event.")
    if assignment.status != AssignmentStatus.ASSIGNED:
        raise AssignmentError("that assignment is not active.")
    if _has_review(assignment):
        raise AssignmentError(
            f"{assignment.judge.user.email} has already started reviewing {assignment.project.name}; "
            "a review is never thrown away."
        )
    assignment.status, assignment.status_changed_at = AssignmentStatus.WITHDRAWN, timezone.now()
    assignment.save(update_fields=["status", "status_changed_at"])
    audit.record(
        action, request=request, subject=event.slug,
        judge=assignment.judge.user.email, project=assignment.project_id, **detail,
    )


def move_assignment(request, event, assignment, to_judge):
    """Withdraw from one judge and give to another, in one step."""
    with transaction.atomic():
        from_email = assignment.judge.user.email
        withdraw_assignment(
            request, event, assignment, action=AuditAction.ASSIGNMENT_MOVED, to=to_judge.user.email,
        )
        _check_can_review(event, to_judge, assignment.project)
        moved = Assignment.objects.create(
            judge=to_judge, project=assignment.project, source=AssignmentSource.MANUAL,
            position=_next_position(to_judge), created_by=request.user,
        )
    return moved, from_email


def decline_assignment(request, assignment, reason):
    """For the judge side: the assigned judge declares a conflict of interest.

    Only the judge themself may decline, only an active assignment, and only before they have
    submitted a review of it. The project goes to the organizer's reassignment queue, and the
    same judge can never be given it again.
    """
    if assignment.judge.user_id != getattr(request.user, "pk", None):
        raise AssignmentError("only the assigned judge can decline this project.")
    if assignment.status != AssignmentStatus.ASSIGNED:
        raise AssignmentError("that assignment is not active.")
    if Score.objects.filter(judge=assignment.judge_id, project=assignment.project_id,
                            submitted_at__isnull=False).exists():
        raise AssignmentError("you have already submitted a review of this project.")
    reason = (reason or "").strip()
    if not reason:
        raise AssignmentError("say briefly what the conflict is; only organizers see it.")
    assignment.status, assignment.status_changed_at = AssignmentStatus.DECLINED, timezone.now()
    assignment.decline_reason = reason[:1000]
    assignment.save(update_fields=["status", "status_changed_at", "decline_reason"])
    audit.record(
        AuditAction.ASSIGNMENT_DECLINED, request=request, subject=assignment.project.event.slug,
        project=assignment.project_id, reason=assignment.decline_reason,
    )
    return assignment


def reassign_unstarted(request, event, judge, *, target=None, max_load=None):
    """A stalled judge: withdraw every assignment they have not started, then top up without
    them. Their started reviews stay theirs."""
    _refuse_if_judging_over(event)
    started = set(Score.objects.filter(judge=judge).values_list("project_id", flat=True))
    pending = Assignment.objects.filter(judge=judge, status=AssignmentStatus.ASSIGNED).exclude(project_id__in=started)
    with transaction.atomic():
        count = 0
        for assignment in pending.select_related("project", "judge__user"):
            withdraw_assignment(request, event, assignment, reason="reassigning a stalled judge")
            count += 1
        round_ = run_assignment(
            request, event, target=target or review_target(event), max_load=max_load,
            exclude_judges=[judge], kind=RoundKind.REASSIGN,
        )
    return count, round_


# ==================================================================================== results

# --- the window -----------------------------------------------------------------------------------
# `judging_closed(event, now)` lives in core.judging (imported above): the one rule, shared with the
# judges' write window and the assignment freeze.


# --- engine input ----------------------------------------------------------------------------------

def build_input(event, *, weights: dict | None = None) -> EngineInput:
    """The event as the engine sees it.

    * projects: the event's *submitted* projects, by id, with their track.
    * reviews: every *submitted* Score of the event, in creation order (the order the engine's
      cross-validation folds depend on). Nothing is silently dropped: a draft is listed in
      `excluded` ("draft, not submitted"), and so is a review of a project that is not submitted
      ("project not submitted") and a review with no values ("no scores").
    * duplicates: submissions the fixture importer folded into another. A review the importer
      moved from a duplicate onto the kept project is given back to the duplicate here (under the
      id "dup:<fixture id>"), so the engine's duplicate policy decides what happens to it -- the
      same as when it reads the fixture file.
    * rubric: the event's criteria and weights (`weights`, a {key: weight} override, is for
      previews only; `compute_snapshot` refuses it on a final).
    * event_id: the slug, from which the default CV seed is derived.
    """
    criteria = list(Criterion.objects.filter(event=event).order_by("order", "key"))
    weights = weights or {}
    unknown = sorted(set(weights) - {c.key for c in criteria})
    if unknown:
        raise InvalidConfig(f"Weights name criteria this event does not have: {', '.join(unknown)}.")
    rubric = Rubric(tuple(
        EngineCriterion(c.key, float(weights.get(c.key, c.weight)), float(c.min_value), float(c.max_value))
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
        review_id = f"{judge_id}:{project_id}"
        if score.submitted_at is None:
            excluded.append(Exclusion("review", review_id, "draft, not submitted"))
            continue
        if project_id not in projects:
            excluded.append(Exclusion("review", review_id, "project not submitted"))
            continue
        items = {item.criterion.key: float(item.value) for item in score.items.all()}
        if not items:
            excluded.append(Exclusion("review", review_id, "no scores"))
            continue
        reviews.append(Review(judge_id, project_id, items, projects[project_id]))
    return EngineInput(event_id=event.slug, reviews=tuple(reviews), rubric=rubric, projects=projects,
                       duplicates=duplicates, excluded=tuple(excluded))


def display_labels(event, inp: EngineInput) -> dict:
    """Readable names for the ids in an engine input, for printed output (never bare database
    ids): a project is "name (fixture id)" where the importer recorded one, else "name (#pk)"; a
    duplicate the importer folded in is "kept name, duplicate (fixture id)"; a judge is their
    email; a track its name."""
    from events.models import EventMembership, Track

    rows = {str(p.pk): p for p in Project.objects.filter(event=event)}
    fixture_ids = {
        str(ref.object_id): ref.external_id
        for ref in FixtureRef.objects.filter(source=FIXTURE_SOURCE, kind=FixtureRef.Kind.PROJECT,
                                             duplicate_of="", object_id__in=[p.pk for p in rows.values()])
    }
    projects = {pk: f"{p.name} ({fixture_ids.get(pk, f'#{pk}')})" for pk, p in rows.items()}
    for dup_id, kept in inp.duplicates.items():
        kept_name = rows[kept].name if kept in rows else f"#{kept}"
        projects[dup_id] = f"{kept_name}, duplicate ({dup_id.removeprefix('dup:')})"
    judges = {str(m.pk): m.user.email for m in EventMembership.objects.filter(event=event).select_related("user")}
    tracks = {str(t.pk): t.name for t in Track.objects.filter(event=event)}
    return {"projects": projects, "judges": judges, "tracks": tracks}


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
    before judging closed); for a final with community_weight > 0, NoVoteForCommunityWeight (the event
    has no vote) and VotingOpen (409 voting_open: it has not closed); InvalidConfig; ConcurrentFinal
    (409: another final froze a tally at the same moment; retry). Each is audited, outside any
    transaction.

    The community part: a final of an event whose vote has closed freezes a new VoteTallySnapshot
    (voting.services.freeze_tally, inside this transaction) and records it; a preview uses the live
    tally and freezes nothing. When there is a tally (or the community weight is above 0),
    `combined` is scoring.engine.combine's ranking under the event's weights.
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
        judge_weight, community_weight = final_weights(event)
        vote = VotingConfig.objects.filter(event=event).first()
        if community_weight > 0 and vote is None:
            refuse(NoVoteForCommunityWeight(
                f"The community weight is {community_weight}, but this event has no vote to count. Set the weight "
                "to 0, or set up voting."), "community weight without a vote")
        if community_weight > 0 and now < vote.closes_at:
            refuse(VotingOpen(f"Community voting closes at {vote.closes_at.isoformat()}; a final result with a "
                              "community weight can only be computed after that."), "voting open")
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
            judge_weight, community_weight = final_weights(event)
            vote = VotingConfig.objects.filter(event=event).first()
            tally, rows = None, None
            if vote is not None and kind == SnapshotKind.FINAL:
                if db_now() >= vote.closes_at:
                    tally = voting_services.freeze_tally(event, actor=actor)
                    rows = tally.rows
                    diagnostics["vote_tally"] = {
                        "id": tally.pk, "previous": tally.previous_id,
                        "changed_since_previous_tally": tally.changed_since_previous,
                        "voided_since_previous": tally.voided_since_previous,
                        "restored_since_previous": tally.restored_since_previous,
                    }
                else:
                    diagnostics["vote_tally"] = None
                    diagnostics["no_tally_reason"] = "voting had not closed: this final has no community part"
            elif vote is not None:
                rows = voting_services.tally_rows(event)
                diagnostics["live_tally"] = rows
            combined = None
            if rows is not None or community_weight > 0:
                scores = {p.project_id: p.score for p in result.projects
                          if p.score is not None and math.isfinite(p.score) and not p.project_id.startswith("dup:")}
                influence = {row["project_id"]: row["influence"] for row in rows or ()}
                combined = combining.to_json(combining.combine(scores, influence, judge_weight, community_weight,
                                                               decimals=cfg.equal_decimals))
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
                diagnostics=diagnostics, vote_tally=tally, combined=combined,
                final_weights={"judge": judge_weight, "community": community_weight},
            )
    except InvalidConfig as error:
        refuse(error, f"invalid: {error.detail}")
    except (ConfigError, EngineError) as error:
        refuse(InvalidConfig(str(error)), f"invalid: {error}")
    except OperationalError as error:
        if getattr(getattr(error, "__cause__", None), "sqlstate", None) == "40001":  # serialization failure
            refuse(ConcurrentFinal("Another final was computed at the same moment. Compute again."), "concurrent final")
        raise
    if tally is not None:
        audit.record(AuditAction.TALLY_FROZEN, request=request, actor=actor, subject=event.slug, tally=tally.pk,
                     snapshot=snapshot.pk, changed_since_previous=tally.changed_since_previous,
                     voided_since_previous=tally.voided_since_previous,
                     restored_since_previous=tally.restored_since_previous)
    audit.record(AuditAction.SNAPSHOT_CREATED, request=request, actor=actor, subject=event.slug,
                 kind=kind, snapshot=snapshot.pk, method=snapshot.method, input_hash=digest,
                 vote_tally=tally.pk if tally else None,
                 final_weights={"judge": judge_weight, "community": community_weight})
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


# --- publishing results ------------------------------------------------------------------------------
#
# A final snapshot becomes the event's result by a Publication (append-only; the Postgres trigger in
# scoring/migrations/0005 is the backstop). Who then sees it is `EventResultSettings.visibility`
# (scoring/results.py reads both). These services take the actor and an `audit.Origin`, never the
# request. Publish and unpublish lock the event row, so two organizers pressing at once are
# serialised: the second sees the first's publication and is refused with a clear 409, rather than
# tripping the partial unique constraint.

def latest_final(event):
    return (ResultSnapshot.objects.filter(event=event, kind=SnapshotKind.FINAL)
            .order_by("-created_at", "-id").first())


def active_publication(event):
    return (Publication.objects.filter(event=event, unpublished_at__isnull=True)
            .select_related("snapshot").first())


def publish_results(event, snapshot_id, *, actor, origin=None) -> Publication:
    """Publish the event's latest final snapshot. Refusals, each audited, in this order:
    PermissionDenied (not an organizer of the event, nor an admin); VotingOpen (409: community voting
    has not closed yet -- checked first, whatever the snapshot); NoSuchSnapshot (404); NotFinal (400);
    NotLatestFinal, FinalPredatesVoteClose (the event has a vote but
    this final has no tally: it was computed before the vote closed), AlreadyPublished (409)."""

    def refuse(error, reason):
        audit.record(AuditAction.RESULTS_PUBLISH_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     operation="publish", snapshot=snapshot_id, reason=reason)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can publish its results."), "not an organizer")
    try:
        with transaction.atomic():
            Event.objects.select_for_update().filter(pk=event.pk).first()
            # The window first (like every deadline): while the vote is open nothing is published,
            # whatever the snapshot.
            voting = VotingConfig.objects.filter(event=event).first()
            if voting is not None and db_now() < voting.closes_at:
                raise VotingOpen(f"Community voting closes at {voting.closes_at.isoformat()}; final results "
                                 "can be published once it has closed.")
            snapshot = ResultSnapshot.objects.filter(event=event, pk=snapshot_id).first()
            if snapshot is None:
                raise NoSuchSnapshot("This event has no such result snapshot.")
            if snapshot.kind != SnapshotKind.FINAL:
                raise NotFinal("Only a final result can be published; this one is a preview.")
            if latest_final(event).pk != snapshot.pk:
                raise NotLatestFinal("A newer final result exists; publish that one instead.")
            if voting is not None and snapshot.vote_tally_id is None:
                raise FinalPredatesVoteClose(
                    f"Final #{snapshot.pk} was computed before community voting closed, so it has no vote "
                    "tally. Compute final results again, then publish that.")
            current = active_publication(event)
            if current is not None:
                raise AlreadyPublished(
                    f"Final #{current.snapshot_id} is already published; unpublish it first.")
            publication = Publication.objects.create(
                event=event, snapshot=snapshot, published_at=db_now(), published_by=actor,
                published_by_email=actor.email,
            )
    except (NoSuchSnapshot, NotFinal, NotLatestFinal, AlreadyPublished, VotingOpen, FinalPredatesVoteClose) as error:
        refuse(error, error.code)
    except IntegrityError:  # the partial unique constraint, if the lock was somehow not enough
        refuse(AlreadyPublished("These results are already published; unpublish them first."), "already_published")
    audit.record(AuditAction.RESULTS_PUBLISHED, origin=origin, actor=actor, subject=event.slug,
                 snapshot=snapshot.pk, publication=publication.pk)
    return publication


def unpublish_results(event, *, actor, origin=None) -> Publication:
    """Take the published result down. The publication row stays (append-only); it records who
    unpublished it and when. The result can be published again (a new publication)."""

    def refuse(error, reason):
        audit.record(AuditAction.RESULTS_PUBLISH_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     operation="unpublish", reason=reason)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can unpublish its results."), "not an organizer")
    try:
        with transaction.atomic():
            Event.objects.select_for_update().filter(pk=event.pk).first()
            publication = (Publication.objects.select_for_update()
                           .filter(event=event, unpublished_at__isnull=True).first())
            if publication is None:
                raise NotPublished("These results are not published.")
            publication.unpublished_at = db_now()
            publication.unpublished_by = actor
            publication.unpublished_by_email = actor.email
            publication.save(update_fields=["unpublished_at", "unpublished_by", "unpublished_by_email"])
    except NotPublished as error:
        refuse(error, error.code)
    audit.record(AuditAction.RESULTS_UNPUBLISHED, origin=origin, actor=actor, subject=event.slug,
                 snapshot=publication.snapshot_id, publication=publication.pk)
    return publication


def result_settings(event) -> EventResultSettings:
    """The event's settings, or the defaults (unsaved) when it has none."""
    return EventResultSettings.objects.filter(event=event).first() or EventResultSettings(event=event)


def set_result_settings(event, *, actor, visibility, winners_top_n, origin=None) -> EventResultSettings:
    """Change who sees the published result and how many overall winners it names. Allowed at any
    time: it changes presentation, never the result."""
    if not is_organizer_of(actor, event):
        raise PermissionDenied("Only the event's organizers can change who sees its results.")
    if visibility not in ResultVisibility.values:
        raise InvalidResultSettings(f"visibility must be one of {', '.join(ResultVisibility.values)}.")
    if not isinstance(winners_top_n, int) or isinstance(winners_top_n, bool)             or not 1 <= winners_top_n <= WINNERS_TOP_N_MAX:
        raise InvalidResultSettings(f"winners_top_n must be a whole number from 1 to {WINNERS_TOP_N_MAX}.")
    with transaction.atomic():
        row, _ = EventResultSettings.objects.select_for_update().get_or_create(event=event)
        before = {"visibility": row.visibility, "winners_top_n": row.winners_top_n}
        row.visibility, row.winners_top_n, row.updated_by = visibility, winners_top_n, actor
        row.save()
    after = {"visibility": visibility, "winners_top_n": winners_top_n}
    if before != after:
        audit.record(AuditAction.RESULT_SETTINGS_CHANGED, origin=origin, actor=actor, subject=event.slug,
                     before=before, after=after)
    return row


# --- the final score's weights ------------------------------------------------------------------------

def final_weights(event):
    """(judge_weight, community_weight): the event's, or 100/0 (judges only)."""
    row = EventScoringConfig.objects.filter(event=event).first()
    return (row.judge_weight, row.community_weight) if row else (100, 0)


def weights_locked(event, now=None):
    """True once judging or voting has opened (by the database clock). The weights decide the
    ranking, so they must be fixed before anyone judges or votes under them."""
    now = now or db_now()
    if now >= event.judging_starts_at:
        return True
    vote = VotingConfig.objects.filter(event=event).first()
    return vote is not None and now >= vote.opens_at


def set_final_weights(event, *, actor, judge_weight, community_weight, origin=None) -> EventScoringConfig:
    """Set the judge/community split of the final score. Whole numbers, each >= 0, summing to 100.
    Refused, audited: PermissionDenied; InvalidWeights (400); WeightsLocked (409 weights_locked) once
    judging or voting has opened -- a Postgres trigger (scoring/migrations/0011) refuses the same."""

    def refuse(error, reason):
        audit.record(AuditAction.WEIGHTS_REFUSED, origin=origin, actor=actor, subject=event.slug, reason=reason,
                     judge_weight=judge_weight, community_weight=community_weight)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can set the final score's weights."), "not an organizer")
    for value in (judge_weight, community_weight):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            refuse(InvalidWeights("Weights are whole numbers, 0 or more."), "not whole numbers")
    if judge_weight + community_weight != 100:
        refuse(InvalidWeights(f"The weights must add up to 100 (they add up to {judge_weight + community_weight})."),
               "not 100")
    problem = None
    with transaction.atomic():
        Event.objects.select_for_update().filter(pk=event.pk).first()
        before = final_weights(event)
        if before != (judge_weight, community_weight):
            if weights_locked(event):
                problem = WeightsLocked("Judging or voting has opened, so the final score's weights are fixed.")
            else:
                row, _ = EventScoringConfig.objects.get_or_create(event=event)
                row.judge_weight, row.community_weight, row.updated_by = judge_weight, community_weight, actor
                row.save()
    if problem:
        refuse(problem, "locked")
    if before != (judge_weight, community_weight):
        audit.record(AuditAction.WEIGHTS_CHANGED, origin=origin, actor=actor, subject=event.slug,
                     before={"judge": before[0], "community": before[1]},
                     after={"judge": judge_weight, "community": community_weight})
    return EventScoringConfig.objects.filter(event=event).first()


@contextmanager
def weights_bypass(reason, *, actor=None, origin=None, subject=""):
    """The one way past the weights-lock trigger, for one block (switched off again on the way out,
    so it cannot leak into an outer transaction). Audited even if the block fails. Used by the demo
    seed only; no page or API uses it."""
    try:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL dogfood.weights_bypass = 'on'")
            try:
                yield
            finally:
                if connection.vendor == "postgresql" and not connection.needs_rollback:
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL dogfood.weights_bypass = 'off'")
    finally:
        audit.record(AuditAction.WEIGHTS_BYPASSED, origin=origin, actor=actor, subject=subject, reason=reason)


# ==================================================================================== the judge side
#
# A judge writes one review per assigned project: a draft (any subset of the criteria) until they
# submit it (every criterion, then `submitted_at` is set; only submitted reviews count anywhere).
# Saving a draft over a submitted review reopens it. Order of checks on every write:
#   1. the judging window (core.judging, 409 judging_not_started / judging_closed) -- first, so a
#      late write is refused as late;
#   2. a live `assigned` Assignment for this judge and project (403 not_assigned);
#   3. the values against the rubric (400 invalid_review).
# A conflict of interest is declared with `decline_assignment` (above), never by editing a review.

CIRCLE_CIRCUMFERENCE = 264  # the progress donut's stroke length (judge/event.html)
VALUE_STEP = Decimal("0.01")  # ScoreItem.value has two decimal places


class ReviewError(Exception):
    """A refused review write, with a sentence the page can show as-is."""

    status = 400
    code = "invalid_review"


class ReviewForbidden(ReviewError):
    status = 403
    code = "not_assigned"


def judge_membership(user, event):
    """`user`'s judge membership in `event`, or None."""
    if user is None or not user.is_authenticated or event is None:
        return None
    return EventMembership.objects.filter(user=user, event=event, role=Role.JUDGE).first()


def live_assignment(membership, project):
    """The judge's active assignment for `project`, or None."""
    return Assignment.objects.filter(judge=membership, project=project, status=AssignmentStatus.ASSIGNED).first()


def judge_queue(membership):
    """The judge's active assignments to submitted projects, in queue order."""
    return (
        Assignment.objects.filter(judge=membership, status=AssignmentStatus.ASSIGNED,
                                  project__status=Status.SUBMITTED)
        .select_related("project__team", "project__track").order_by("position", "id")
    )


def terminal_bar(done, total, width=32):
    """A text progress bar: "████░░░░  25% | 2/8"."""
    pct = 0.0 if total <= 0 else max(0.0, min(100.0, done / total * 100.0))
    filled = int(round(pct / 100.0 * width))
    return f"{'█' * filled}{'░' * (width - filled)}  {pct:>3.0f}% | {done}/{total}"


def weighted_rating(criteria, items):
    """The judge's own weighted average for display, over the criteria scored (None if none)."""
    scored = [(c, items[c.pk]) for c in criteria if c.pk in items]
    if not scored:
        return None
    total = sum(Decimal(c.weight) for c, _ in scored)
    if total <= 0:
        return sum(Decimal(v) for _, v in scored) / len(scored)
    return sum(Decimal(v) * Decimal(c.weight) for c, v in scored) / total


def judge_progress(membership):
    """The judge console: every active assignment with its review state, and the totals.

    Read-only: nothing is created here (an event without a rubric simply has no criteria yet)."""
    event = membership.event
    criteria = with_shares(Criterion.objects.filter(event=event).order_by("order", "key"))
    queue = list(judge_queue(membership))
    scores = {
        s.project_id: s
        for s in Score.objects.filter(judge=membership, project__in=[a.project_id for a in queue])
        .prefetch_related("items")
    }
    rows, submitted, drafts = [], 0, 0
    for assignment in queue:
        score = scores.get(assignment.project_id)
        items = {i.criterion_id: i.value for i in score.items.all()} if score else {}
        if score is None:
            status = "not started"
        elif score.submitted_at:
            status = "submitted"
            submitted += 1
        else:
            status = "draft"
            drafts += 1
        rating = weighted_rating(criteria, items)
        rows.append({
            "assignment": assignment, "project": assignment.project, "score": score, "status": status,
            "rating": f"{rating:.2f}" if rating is not None else "-",
        })
    total = len(queue)
    pct = (submitted / total * 100.0) if total else 0.0
    return {
        "event": event, "criteria": criteria, "projects": rows, "total_projects": total,
        "submitted": submitted, "drafts": drafts, "progress_pct": round(pct, 1),
        "dash_offset": int(round(CIRCLE_CIRCUMFERENCE * (1.0 - pct / 100.0))),
        "terminal_bar": terminal_bar(submitted, total),
    }


def _parse_values(criteria, values, *, require_all):
    """{criterion key: Decimal} from submitted values; empty strings mean "not scored yet"."""
    by_key = {c.key: c for c in criteria}
    values = {k: v for k, v in (values or {}).items() if v is not None and str(v).strip() != ""}
    unknown = sorted(set(values) - set(by_key))
    if unknown:
        raise ReviewError(f"not criteria of this event: {', '.join(unknown)}.")
    parsed = {}
    for key, raw in values.items():
        criterion = by_key[key]
        if isinstance(raw, bool):
            raise ReviewError(f"'{criterion.label}': {raw!r} is not a number.")
        try:
            value = Decimal(str(raw).strip())
        except Exception:
            raise ReviewError(f"'{criterion.label}': {raw!r} is not a number.") from None
        if not value.is_finite() or value != value.quantize(VALUE_STEP):
            raise ReviewError(f"'{criterion.label}': use a number with at most two decimal places.")
        if not criterion.min_value <= value <= criterion.max_value:
            raise ReviewError(
                f"'{criterion.label}': {value.normalize():f} is outside "
                f"{criterion.min_value}-{criterion.max_value}."
            )
        parsed[key] = value
    if require_all:
        missing = [c.label for c in criteria if c.key not in parsed]
        if missing:
            raise ReviewError(f"score every criterion before submitting; missing: {', '.join(missing)}.")
    return parsed


def save_review(request, membership, project, values, comment="", *, submit=False):
    """Save the judge's review of `project` as a draft, or submit it. Returns the Score.

    Refusals: core.judging.JudgingNotOpen (409), ReviewForbidden (403), ReviewError (400).
    Every write is audited with the values before and after, so the audit log is the revision
    history."""
    from core.judging import check_judging_window

    event = membership.event
    check_judging_window(request, event, action="submit review" if submit else "save review")
    if membership.role != Role.JUDGE:
        raise ReviewForbidden("only judges review projects.")
    if (project.event_id != event.pk or project.status != Status.SUBMITTED
            or live_assignment(membership, project) is None):
        raise ReviewForbidden("this project is not in your queue.")
    criteria = list(Criterion.objects.filter(event=event).order_by("order", "key"))
    if not criteria:
        raise ReviewError("the organizers have not set the rubric yet.")
    parsed = _parse_values(criteria, values, require_all=submit)
    comment = (comment or "").strip()

    with transaction.atomic():
        score, created = Score.objects.select_for_update().get_or_create(judge=membership, project=project)
        before = {i.criterion.key: str(i.value) for i in score.items.select_related("criterion")}
        was_submitted = score.submitted_at is not None
        score.comment = comment
        score.submitted_at = db_now() if submit else None
        score.save()
        by_key = {c.key: c for c in criteria}
        ScoreItem.objects.filter(score=score).exclude(criterion__key__in=list(parsed)).delete()
        for key, value in parsed.items():
            ScoreItem.objects.update_or_create(score=score, criterion=by_key[key], defaults={"value": value})

    if submit:
        action = AuditAction.SCORE_SUBMITTED
    elif was_submitted:
        action = AuditAction.SCORE_REOPENED
    else:
        action = AuditAction.SCORE_SAVED
    audit.record(
        action, request=request, subject=event.slug, project=project.pk,
        values={k: str(v) for k, v in parsed.items()}, previous=None if created else before,
        comment_length=len(comment),
    )
    return score


def reviews_of(user):
    """Every review `user` wrote as a judge, in any event (their own only)."""
    return (
        Score.objects.filter(judge__user=user, judge__role=Role.JUDGE)
        .select_related("judge__event", "project").prefetch_related("items__criterion").order_by("pk")
    )
