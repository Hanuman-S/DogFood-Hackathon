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

    # "dogfood-fixtures" for the organizers' file; "bundle:<sha256>:<new slug>" for refs an event
    # bundle brought in (unique per import, so importing one bundle twice cannot clash).
    source = models.CharField(max_length=160, default="dogfood-fixtures")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    external_id = models.CharField(max_length=120)
    object_id = models.BigIntegerField()
    # Set when this external id was folded into another row (e.g. a duplicate submission).
    duplicate_of = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # The event the referenced row belongs to. Every kind but `user` is event-scoped (a `judge` ref
    # points at an EventMembership), and scoring finds an event's refs by this, whatever their
    # source. CASCADE: a ref means nothing once its event is gone.
    event = models.ForeignKey("events.Event", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="fixture_refs")

    EVENT_SCOPED = (Kind.EVENT, Kind.TRACK, Kind.TEAM, Kind.PROJECT, Kind.SCORE, Kind.JUDGE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "kind", "external_id"], name="fixture_ref_unique"),
            models.CheckConstraint(condition=models.Q(kind="user") | models.Q(event__isnull=False),
                                   name="fixture_ref_event_scoped_has_event"),
        ]

    def __str__(self):
        return f"{self.kind}:{self.external_id} -> {self.object_id}"
