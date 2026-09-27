"""Where imported rows came from.

Every row the importer creates gets a `FixtureRef(kind, external_id) -> object_id`. That makes
the import idempotent (a second run finds the ref and creates nothing) without adding an
`external_id` column to every table, and it lets two external ids point at one row -- which is
how a duplicate submission is recorded rather than silently dropped.
"""

from django.db import models
from django.utils import timezone


class FixtureRef(models.Model):
    class Kind(models.TextChoices):
        EVENT = "event"
        TRACK = "track"
        USER = "user"
        TEAM = "team"
        PROJECT = "project"
        SCORE = "score"
        # a fixture judge id -> that judge's EventMembership in the imported event
        JUDGE = "judge"

    source = models.CharField(max_length=60, default="dogfood-fixtures")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    external_id = models.CharField(max_length=120)
    object_id = models.BigIntegerField()
    # Set when this external id was folded into another row (e.g. a duplicate submission).
    duplicate_of = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "kind", "external_id"], name="fixture_ref_unique"),
        ]

    def __str__(self):
        return f"{self.kind}:{self.external_id} -> {self.object_id}"
