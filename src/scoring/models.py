"""Scoring tables: the rubric, the reviews, and who is asked to review what.

Ownership inside T2 (so three people can work here without stepping on each other):

* **Organizer side** -- `Criterion` (the rubric editor), `AssignmentRound` and `Assignment`
  (who reviews what), and `Score.submitted_at` as read by the progress dashboard. Writes go
  through `scoring/services.py`.
* **Judge side** -- writes `Score` / `ScoreItem` and sets `Score.submitted_at`. A judge may
  score a project only while they hold an `assigned` Assignment for it, and declines one only
  through `scoring.services.decline_assignment` (never by editing `status` directly).
* **Scoring engine** -- reads submitted scores and weights (`scoring/engine/`, through
  `scoring.services.build_input`); its own tables are below: `EventScoringConfig`,
  `ResultSnapshot` (immutable), `Publication` (append-only) and `EventResultSettings` (who sees
  a published result).

Scores themselves are read only by organizers; the public sees a result only once an organizer
publishes a final snapshot with a public visibility (`scoring.results`). The gallery and the project
pages never show scores. `JUDGING.md` states what is and is not built.

They are defined now rather than in T2 because the fixture ships 126 reviews and discarding
them on import would mean re-importing later against a schema designed without them in view.
The shape below is driven by what the fixture actually contains:

* Three criteria keys (`functionality`, `quality`, `innovation`), so criteria are rows rather
  than columns -- an organizer configures their own rubric in T2 without a migration.
* A per-criterion value plus one comment per review, hence the Score / ScoreItem split.
* Between 2 and 5 reviews per project, and 1 to 11 reviews per judge. Nothing here assumes a
  balanced matrix, because the real data is not balanced.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Criterion(models.Model):
    """One line of an event's scoring rubric.

    `weight` is stored per criterion because the brief's central complaint about existing
    platforms is that "the market leader cannot weight judging criteria". Weighting has to be
    data, not code.

    Weights are **relative**: a criterion's share is its weight over the sum of the event's
    weights (1 / 1 / 1 = exact thirds). They must be above 0 (a CHECK constraint).

    **Locked from the submission close onward** (`scoring.services.rubric_locked`): weights, the
    min/max of the scale and the set of criteria cannot change once judging can start, because
    they change the ranking. Labels and descriptions stay editable, and every edit is audited.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="criteria")
    key = models.SlugField(
        max_length=60,
        help_text="Stable machine name, e.g. 'functionality'. Matches the fixture's criteria keys.",
    )
    label = models.CharField(max_length=120)
    weight = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=1,
        validators=[MinValueValidator(Decimal("0.001"))],
        help_text="Relative weight, above 0: the criterion's share of the score is its weight over "
        "the sum of the event's weights (1 / 1 / 1 = exact thirds). Decimal, not float, so what "
        "the organizer typed is exactly what is stored.",
    )
    # Always 1 and 5 (a CHECK constraint). Kept as columns because the judge portal, the
    # engine input and the audit log read the scale from the criterion.
    min_value = models.SmallIntegerField(default=1)
    max_value = models.SmallIntegerField(default=5)
    order = models.PositiveSmallIntegerField(default=0)
    description = models.TextField(
        blank=True, help_text="What judges should look for under this criterion."
    )
    # {"1": "Does not run", ..., "5": "Works end to end, polished"}: a written anchor per score
    # level, so a 3 means the same thing to every judge. Keys are the levels as strings.
    level_descriptions = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "order", "key"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="criterion_unique_key_per_event"),
            # Every criterion is scored 1-5: the scoring engine's tuning (ridge lambdas, variance
            # floor, near-flat threshold) is in score units sized for that scale.
            models.CheckConstraint(
                condition=Q(min_value=1, max_value=5),
                name="criterion_scale_is_1_to_5",
            ),
            models.CheckConstraint(condition=Q(weight__gt=0), name="criterion_weight_positive"),
        ]
        verbose_name_plural = "criteria"

    def __str__(self) -> str:
        return f"{self.label} (weight {self.weight.normalize():f})"


class Score(models.Model):
    """One judge's review of one project: the comment, plus a ScoreItem per criterion.

    The judge is referenced as an `EventMembership`, not a `User`. That is deliberate: a score
    is only meaningful within the event whose rubric it was given against, and pointing at the
    membership makes "this judge, in this event" a single column rather than a pair that could
    disagree. It also means revoking someone's judge role cascades to their scores, rather than
    leaving orphaned reviews attributed to a person who is no longer a judge.
    """

    judge = models.ForeignKey(
        "events.EventMembership", on_delete=models.CASCADE, related_name="scores"
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="scores"
    )
    # Empty for 51 of the fixture's 126 reviews, so blank must be allowed.
    comment = models.TextField(blank=True)
    # Null while the judge is still drafting. Only submitted reviews count: for progress, for
    # normalization and for results. Imported fixture reviews are submitted (at import time).
    submitted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "judge"]
        constraints = [
            # One review per judge per project. A judge who changes their mind edits their
            # existing review rather than adding a second one, which is what makes
            # "reviews per project" a countable, auditable number.
            models.UniqueConstraint(fields=["judge", "project"], name="score_unique_judge_project"),
        ]
        indexes = [
            models.Index(fields=["project"], name="score_project_idx"),
            models.Index(fields=["judge"], name="score_judge_idx"),
        ]

    def __str__(self) -> str:
        return f"score by {self.judge_id} for {self.project_id}"

    def clean(self):
        from accounts.roles import Role

        if self.judge_id and self.judge.role != Role.JUDGE:
            raise ValidationError({"judge": "Scores can only be attached to a judge membership."})
        if self.judge_id and self.project_id and self.judge.event_id != self.project.event_id:
            raise ValidationError({"project": "The project belongs to a different event."})


class ScoreItem(models.Model):
    """The value a judge gave for one criterion within one review."""

    score = models.ForeignKey(Score, on_delete=models.CASCADE, related_name="items")
    criterion = models.ForeignKey(Criterion, on_delete=models.CASCADE, related_name="items")
    value = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Within the criterion's min/max. Decimal so a 0.5-step rubric is possible "
        "in T2 without a migration.",
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["score", "criterion__order"]
        constraints = [
            models.UniqueConstraint(
                fields=["score", "criterion"], name="scoreitem_unique_per_criterion"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.criterion_id}={self.value}"

    def clean(self):
        """Range is validated here rather than by a CHECK constraint.

        The bounds live on `Criterion`, a different table, so a CHECK cannot see them. The
        importer and (in T2) the scoring service both validate through this method.
        """
        if self.criterion_id and self.value is not None:
            if not (self.criterion.min_value <= self.value <= self.criterion.max_value):
                raise ValidationError(
                    {
                        "value": (
                            f"{self.value} is outside the allowed range "
                            f"{self.criterion.min_value}-{self.criterion.max_value} "
                            f"for {self.criterion.key}."
                        )
                    }
                )
        if self.score_id and self.criterion_id and self.score.judge.event_id != self.criterion.event_id:
            raise ValidationError({"criterion": "The criterion belongs to a different event."})


# --- assignment -------------------------------------------------------------------------------


class AssignmentStatus(models.TextChoices):
    ASSIGNED = "assigned", "Assigned"
    # The judge declared a conflict of interest; the organizer's queue reassigns the review.
    DECLINED = "declined_conflict", "Declined (conflict of interest)"
    # Taken away by an organizer (moved to another judge, or removed). Kept, not deleted, so
    # the history of who was asked to review what stays readable.
    WITHDRAWN = "withdrawn", "Withdrawn by an organizer"


# A judge-project pair may have at most one row in one of these states at a time.
LIVE_STATUSES = (AssignmentStatus.ASSIGNED, AssignmentStatus.DECLINED)


class AssignmentSource(models.TextChoices):
    IMPORT = "import", "Imported with an existing review"
    AUTO = "auto", "Automatic assignment"
    MANUAL = "manual", "Added by an organizer"


class RoundKind(models.TextChoices):
    INITIAL = "initial", "Initial assignment"
    TOP_UP = "top_up", "Top-up to the review target"
    REASSIGN = "reassign", "Reassignment of declined or stalled reviews"


class AssignmentRound(models.Model):
    """One run of the automatic assignment: its parameters, its random seed and what it found.

    The assignment is randomised within the hard rules (track, conflict of interest, load), so
    nobody can predict or steer which judge sees which project. The seed is stored, so the same
    round can be re-run and produces exactly the same assignments.
    """

    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, related_name="assignment_rounds"
    )
    kind = models.CharField(max_length=12, choices=RoundKind.choices)
    seed = models.BigIntegerField()
    target_reviews = models.PositiveSmallIntegerField(
        help_text="Reviews each project should end up with."
    )
    max_load = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Most live assignments one judge may hold; empty = no cap."
    )
    # Counts, capacity and connectivity warnings, projects left short: what the page shows.
    summary = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} (seed {self.seed})"


class Assignment(models.Model):
    """A request for one judge (a judge membership) to review one project."""

    judge = models.ForeignKey(
        "events.EventMembership", on_delete=models.CASCADE, related_name="assignments"
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="assignments"
    )
    round = models.ForeignKey(
        AssignmentRound, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assignments",
    )
    source = models.CharField(max_length=10, choices=AssignmentSource.choices)
    status = models.CharField(
        max_length=20, choices=AssignmentStatus.choices, default=AssignmentStatus.ASSIGNED
    )
    # The project's place in this judge's queue. Shuffled at assignment time, so no project is
    # always reviewed first (or last) -- position bias.
    position = models.PositiveIntegerField(default=0)
    decline_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    status_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["judge", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["judge", "project"],
                condition=Q(status__in=[s.value for s in LIVE_STATUSES]),
                name="assignment_one_live_per_judge_project",
            ),
            models.CheckConstraint(
                condition=Q(status__in=AssignmentStatus.values),
                name="assignment_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(source__in=AssignmentSource.values),
                name="assignment_source_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"], name="assignment_project_idx"),
            models.Index(fields=["judge", "status"], name="assignment_judge_idx"),
        ]

    def __str__(self):
        return f"{self.judge_id} -> {self.project_id} ({self.status})"

    def clean(self):
        from accounts.roles import Role

        if self.judge_id and self.judge.role != Role.JUDGE:
            raise ValidationError({"judge": "Only a judge membership can be assigned a project."})
        if self.judge_id and self.project_id and self.judge.event_id != self.project.event_id:
            raise ValidationError({"project": "The project belongs to a different event."})

# --- results ------------------------------------------------------------------------------------


class EventScoringConfig(models.Model):
    """An event's engine configuration: overrides on top of `scoring.engine.config.EngineConfig`'s
    defaults (primary method, comparison methods, filters...). No row means the defaults.

    Written only through `scoring.services.set_engine_config`, which refuses once judging has
    closed: a final result always uses this configuration, so it must stop moving by then.
    """

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="scoring_config")
    overrides = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"scoring config for {self.event_id}"


class SnapshotKind(models.TextChoices):
    PREVIEW = "preview", "Preview"
    FINAL = "final", "Final"


class SnapshotImmutable(Exception):
    pass


class ResultSnapshot(models.Model):
    """One computed ranking of an event, exactly as it was computed. **Immutable.**

    Created only by `scoring.services.compute_snapshot`; there is no update path anywhere.
    `save()` refuses to update an existing row, and a Postgres trigger rejects every UPDATE
    (scoring/migrations/0005), so a stored result cannot be edited even by raw SQL. DELETE is
    allowed, so a snapshot goes with its event.

    * preview: computable at any time by the event's organizers; never publishable.
    * final: only once judging has closed, always with the event's own configuration and weights.
      Several finals are allowed; each records the one before it.

    `engine_config` is the resolved configuration (the seed actually used, the lambdas chosen per
    component); `rubric` is the criteria and weights the result was computed with; `input_hash`
    is the sha256 of the exact engine input. `created_by` is PROTECT, not SET_NULL: SET_NULL
    would be an UPDATE, which the trigger refuses. The email is kept too, as the audit log does.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="result_snapshots")
    kind = models.CharField(max_length=10, choices=SnapshotKind.choices)
    created_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_by_email = models.CharField(max_length=254)
    method = models.CharField(max_length=40)
    method_version = models.CharField(max_length=20)
    engine_config = models.JSONField()
    rubric = models.JSONField()
    input_hash = models.CharField(max_length=64)
    result = models.JSONField()
    comparison = models.JSONField()
    diagnostics = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["event", "kind", "created_at"], name="snapshot_event_kind_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=SnapshotKind.values), name="snapshot_kind_valid"),
        ]

    def __str__(self):
        return f"{self.kind} snapshot {self.pk} of {self.event_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise SnapshotImmutable("A result snapshot is immutable; compute a new one instead.")
        super().save(*args, **kwargs)


class Publication(models.Model):
    """Which final snapshot is the event's published result. **Append-only.**

    Written only by `scoring.services.publish_results` / `unpublish_results` (audited). Enforced in Postgres by a trigger (scoring/migrations/0005): a publication may only point
    at a *final* snapshot of the *same* event, and once written the only change allowed is
    setting `unpublished_at` / `unpublished_by` once. A partial unique constraint allows one
    active (not unpublished) publication per event. `clean()` checks the same on SQLite.

    `snapshot` is RESTRICT: a published snapshot cannot be deleted on its own, but the whole
    event can still be deleted (the publication goes with it). User FKs are PROTECT, emails kept.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="publications")
    snapshot = models.ForeignKey(ResultSnapshot, on_delete=models.RESTRICT, related_name="publications")
    published_at = models.DateTimeField()
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    published_by_email = models.CharField(max_length=254)
    unpublished_at = models.DateTimeField(null=True, blank=True)
    unpublished_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    unpublished_by_email = models.CharField(max_length=254, blank=True)

    class Meta:
        ordering = ["-published_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event"], condition=Q(unpublished_at__isnull=True),
                name="publication_one_active_per_event",
            ),
            models.CheckConstraint(
                condition=Q(unpublished_at__isnull=True, unpublished_by__isnull=True)
                | Q(unpublished_at__isnull=False, unpublished_by__isnull=False),
                name="publication_unpublished_fields_together",
            ),
        ]

    def __str__(self):
        return f"publication of snapshot {self.snapshot_id}"

    def clean(self):
        if self.snapshot_id:
            if self.snapshot.kind != SnapshotKind.FINAL:
                raise ValidationError({"snapshot": "Only a final snapshot can be published."})
            if self.snapshot.event_id != self.event_id:
                raise ValidationError({"snapshot": "The snapshot belongs to a different event."})


WINNERS_TOP_N_MAX = 50


class ResultVisibility(models.TextChoices):
    PUBLIC_FULL = "public_full", "Public: the full ranking"
    PUBLIC_WINNERS = "public_winners", "Public: the winners only"
    PRIVATE = "private", "Private: organizers and admins only"


class EventResultSettings(models.Model):
    """Who may see an event's published result, and how many overall winners it names.

    Kept apart from `EventScoringConfig` on purpose: this is presentation, not an input to the
    engine, so changing it never makes a final result look computed under a different
    configuration. Written only through `scoring.services.set_result_settings` (audited). No row
    means the defaults: private, top 3.

    Winners = the top `winners_top_n` places overall (a place shared by an exact tie brings in
    everyone on it) + the top project of each track.
    """

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="result_settings")
    visibility = models.CharField(
        max_length=20, choices=ResultVisibility.choices, default=ResultVisibility.PRIVATE,
    )
    winners_top_n = models.PositiveSmallIntegerField(default=3)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(visibility__in=ResultVisibility.values), name="result_visibility_valid",
            ),
            models.CheckConstraint(
                condition=Q(winners_top_n__gte=1, winners_top_n__lte=WINNERS_TOP_N_MAX),
                name="result_winners_top_n_range",
            ),
        ]

    def __str__(self):
        return f"result settings for {self.event_id}"
