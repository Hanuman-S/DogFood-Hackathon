"""Scoring tables.

**Scope warning.** Criterion / Score / ScoreItem are populated by the fixture importer. The
scoring engine (`scoring/engine/`, through `scoring/services.py`) reads them to compute result
snapshots; nothing writes a score outside the importer yet, and no page shows a score or a
result to anyone. There is no judging UI, no assignment and no export. `JUDGING.md` says so.

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
        validators=[MinValueValidator(0)],
        help_text="Relative weight in the weighted average. Decimal, not float: a rubric that "
        "sums to 1.000 must still sum to 1.000 after being read back.",
    )
    min_value = models.SmallIntegerField(default=1)
    max_value = models.SmallIntegerField(default=5)
    order = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "order", "key"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="criterion_unique_key_per_event"),
            models.CheckConstraint(
                condition=Q(min_value__lt=models.F("max_value")),
                name="criterion_min_below_max",
            ),
        ]
        verbose_name_plural = "criteria"

    def __str__(self) -> str:
        return f"{self.label} (x{self.weight})"


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
    (scoring/migrations/0003), so a stored result cannot be edited even by raw SQL. DELETE is
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

    Only the model and its guarantees exist; there is no publishing service or page yet.
    Enforced in Postgres by a trigger (scoring/migrations/0003): a publication may only point
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
