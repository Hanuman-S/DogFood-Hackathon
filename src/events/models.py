"""Events, tracks, prizes, memberships and custom questions.

**Roles are per event.** That is the central decision in this schema. A global "is a judge"
column would be wrong: the same person judges one hackathon and competes in the next, and an
organizer's powers must stop at the edge of their own event. So role lives on
`EventMembership`, and the only platform-wide flags are the two on `accounts.User`.

The conflict-of-interest rule -- nobody is both a competitor and staff in the same event -- is
enforced in the database, not only in the service layer. See `EventMembership.Meta`.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case, Q, Value, When
from django.utils.text import slugify

from core import clock


class Role(models.TextChoices):
    PARTICIPANT = "participant", "Participant"
    JUDGE = "judge", "Judge"
    ORGANIZER = "organizer", "Organizer"


# Roles that compete, and roles that run or assess the event. The conflict-of-interest rule is
# exactly "not both sides at once", so the two sides are named once, here, and every other
# reference derives from these.
COMPETITOR_ROLES = frozenset({Role.PARTICIPANT})
STAFF_ROLES = frozenset({Role.JUDGE, Role.ORGANIZER})

SIDE_COMPETITOR = "competitor"
SIDE_STAFF = "staff"


class EventQuerySet(models.QuerySet):
    def public_gallery(self):
        """Events whose gallery a stranger may browse."""
        return self.filter(gallery_public=True)

    def visible_to(self, user):
        """Events a caller may see listed at all.

        A non-public event is not secret from the people running or entering it -- hiding an
        event from its own organizer would be absurd -- but it is invisible to everyone else.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return self.filter(gallery_public=True)
        if getattr(user, "is_platform_admin", False):
            return self
        return self.filter(
            Q(gallery_public=True) | Q(memberships__user=user) | Q(created_by=user)
        ).distinct()


class Event(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=200,
        unique=True,
        help_text="URL segment, e.g. /events/sample-hack-2026.",
    )
    description = models.TextField(blank=True)

    # --- the timeline. All UTC, always. ---
    starts_at = models.DateTimeField(help_text="When the event itself begins (UTC).")
    submissions_open_at = models.DateTimeField(
        help_text="Writes are refused before this instant (UTC)."
    )
    submissions_close_at = models.DateTimeField(
        help_text="Writes are refused from this instant onwards (UTC). The instant itself is closed."
    )
    judging_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="End of the judging window (UTC). Unused in T1; T2 enforces it.",
    )

    max_team_size = models.PositiveSmallIntegerField(default=4)
    gallery_public = models.BooleanField(
        default=True,
        help_text="When false, submitted projects are not shown in the public gallery.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_events",
        null=True,
        blank=True,
        help_text="Becomes the event's first organizer. Null only for imported events.",
    )

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-submissions_close_at"]
        constraints = [
            # The ordering the brief specifies, enforced by the database as well as by
            # `clean()`. A form is not the only way rows arrive -- imports and the shell also
            # write -- and an event whose window closes before it opens would make every
            # deadline comparison meaningless.
            models.CheckConstraint(
                condition=Q(starts_at__lte=models.F("submissions_open_at")),
                name="event_starts_before_submissions_open",
            ),
            models.CheckConstraint(
                condition=Q(submissions_open_at__lt=models.F("submissions_close_at")),
                name="event_submissions_open_before_close",
            ),
            models.CheckConstraint(
                condition=Q(judging_ends_at__isnull=True)
                | Q(judging_ends_at__gte=models.F("submissions_close_at")),
                name="event_judging_ends_after_submissions_close",
            ),
            models.CheckConstraint(
                condition=Q(max_team_size__gte=1),
                name="event_max_team_size_at_least_one",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        """Same rules as the DB constraints, reported as friendly field errors.

        Duplicated deliberately: the constraint is the guarantee, this is the error message.
        Relying only on the constraint would surface an IntegrityError to an organizer who
        simply typed the dates in the wrong order.
        """
        errors = {}
        if self.starts_at and self.submissions_open_at and self.starts_at > self.submissions_open_at:
            errors["submissions_open_at"] = "Submissions cannot open before the event starts."
        if (
            self.submissions_open_at
            and self.submissions_close_at
            and self.submissions_open_at >= self.submissions_close_at
        ):
            errors["submissions_close_at"] = (
                "Submissions must close strictly after they open."
            )
        if (
            self.judging_ends_at
            and self.submissions_close_at
            and self.judging_ends_at < self.submissions_close_at
        ):
            errors["judging_ends_at"] = "Judging cannot end before submissions close."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.build_slug(self.name)
        super().save(*args, **kwargs)

    @staticmethod
    def build_slug(name: str) -> str:
        """Derive a unique slug from a name.

        Kept as a static helper rather than hidden in `save()` so the importer can ask for the
        slug it is about to create and record it in the import report.
        """
        base = slugify(name) or "event"
        candidate = base
        suffix = 2
        while Event.objects.filter(slug=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    # --- window helpers. These are for display; enforcement is core.deadlines. ---

    @property
    def submissions_open(self) -> bool:
        from core.deadlines import submissions_are_open

        return submissions_are_open(self)

    @property
    def submissions_have_closed(self) -> bool:
        return clock.now() >= self.submissions_close_at


class Track(models.Model):
    """A category a project competes in. Judges are assigned per track."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    name = models.CharField(max_length=200)
    # Blank for every imported track: the organizer fixture supplies only id and name, and
    # inventing descriptions would misrepresent their data as ours.
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["event", "name"], name="track_unique_name_per_event"),
        ]

    def __str__(self) -> str:
        return self.name


class Prize(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    track = models.ForeignKey(
        Track,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="prizes",
        help_text="Null for an event-wide prize.",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    value_text = models.CharField(
        max_length=120,
        blank=True,
        help_text="Free text, e.g. '800 USD'. Not a number: prizes are often not cash.",
    )
    order = models.PositiveSmallIntegerField(default=0, help_text="Display rank.")

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "order", "name"]

    def __str__(self) -> str:
        return self.name


class EventMembershipQuerySet(models.QuerySet):
    def participants(self):
        return self.filter(role=Role.PARTICIPANT)

    def judges(self):
        return self.filter(role=Role.JUDGE)

    def organizers(self):
        return self.filter(role=Role.ORGANIZER)


class EventMembership(models.Model):
    """A user's role in one event. A user may hold more than one role, within limits.

    Allowed: judge + organizer (a small hackathon's organizer often also judges).
    Forbidden: participant + judge, or participant + organizer -- that is the
    conflict-of-interest rule, and it is enforced by a database constraint rather than by
    convention. See `Meta.constraints`.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_memberships"
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)

    # Which side of the conflict-of-interest line this role sits on. A stored generated column
    # (computed by Postgres, not by application code) so that the exclusion constraint below
    # can compare sides without trusting anything Python wrote.
    side = models.GeneratedField(
        expression=Case(
            When(role=Role.PARTICIPANT, then=Value(SIDE_COMPETITOR)),
            default=Value(SIDE_STAFF),
        ),
        output_field=models.CharField(max_length=16),
        db_persist=True,
    )

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventMembershipQuerySet.as_manager()

    class Meta:
        ordering = ["event", "role", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event", "role"], name="membership_unique_user_event_role"
            ),
            # The conflict-of-interest rule, in the database.
            #
            # A CHECK constraint cannot express it, because it depends on *other rows*. A
            # unique index cannot either: we must allow two staff rows (judge + organizer)
            # while forbidding one competitor row alongside any staff row. That is an
            # exclusion constraint -- "no two rows for the same (user, event) may disagree
            # about `side`" -- which Postgres supports directly.
            #
            # It needs the btree_gist extension for the `<>` operator on scalar types; the
            # migration creates it, and DATA-MODEL.md notes the requirement for self-hosters.
            ExclusionConstraint(
                name="membership_no_competitor_and_staff",
                expressions=[
                    ("user", RangeOperators.EQUAL),
                    ("event", RangeOperators.EQUAL),
                    ("side", RangeOperators.NOT_EQUAL),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} as {self.role} in {self.event.slug}"

    @property
    def is_staff_side(self) -> bool:
        return self.role in STAFF_ROLES


class JudgeTrack(models.Model):
    """Which tracks a judge is responsible for.

    T1 imports these from the fixture and displays them; T2 uses them to build assignments.
    """

    membership = models.ForeignKey(
        EventMembership, on_delete=models.CASCADE, related_name="judge_tracks"
    )
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="judge_tracks")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["membership", "track"]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "track"], name="judgetrack_unique_pair"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.membership.user.email} judges {self.track.name}"

    def clean(self):
        """Only a judge membership may carry tracks.

        Not expressible as a database constraint without denormalizing `role` into this table,
        which would then need keeping in step. Since rows are only ever created by
        `events.services`, a validation check plus the service-layer guard is the honest
        trade-off -- and DATA-MODEL.md says so rather than implying a constraint exists.
        """
        if self.membership_id and self.membership.role != Role.JUDGE:
            raise ValidationError(
                {"membership": "Tracks can only be assigned to a judge membership."}
            )
        if self.membership_id and self.track_id and self.membership.event_id != self.track.event_id:
            raise ValidationError({"track": "The track belongs to a different event."})


class QuestionKind(models.TextChoices):
    SHORT_TEXT = "short_text", "Short text"
    LONG_TEXT = "long_text", "Long text"
    URL = "url", "URL"
    CHOICE = "choice", "Choice"
    BOOLEAN = "boolean", "Yes / no"


class CustomQuestion(models.Model):
    """An organizer-defined question added to the submission form."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    prompt = models.CharField(max_length=300)
    kind = models.CharField(max_length=20, choices=QuestionKind.choices)
    choices = models.JSONField(
        default=list,
        blank=True,
        help_text="Allowed values, for kind=choice. Ignored otherwise.",
    )
    required = models.BooleanField(
        default=False, help_text="Required questions must be answered to submit, not to draft."
    )
    order = models.PositiveSmallIntegerField(default=0)
    show_in_gallery = models.BooleanField(
        default=False,
        help_text="When true, the answer is shown publicly on the project page.",
    )

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "order", "id"]

    def __str__(self) -> str:
        return self.prompt

    def clean(self):
        if self.kind == QuestionKind.CHOICE:
            values = [str(c).strip() for c in (self.choices or []) if str(c).strip()]
            if len(values) < 2:
                raise ValidationError(
                    {"choices": "A choice question needs at least two options."}
                )
            if len(set(values)) != len(values):
                raise ValidationError({"choices": "Options must be distinct."})
        elif self.choices:
            raise ValidationError(
                {"choices": f"Options are only meaningful for a choice question, not {self.kind}."}
            )
