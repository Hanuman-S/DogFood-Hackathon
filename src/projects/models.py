"""Projects, their images, tags and answers to an event's custom questions.

Three things here are worth reading closely:

* **Draft vs submitted.** A draft is invisible to everyone but its own team and the event's
  organizers. It never appears in the gallery, and a draft that was never submitted before the
  deadline stays a draft forever -- there is no code path that promotes one afterwards.
* **Duplicate submissions are kept, not deleted.** The organizer fixture contains a team that
  submitted the same project twice, three minutes before the deadline. Deleting the later row
  would destroy evidence and the scores attached to it. Instead the later row points at the
  earlier one through `duplicate_of`, the earlier one stays canonical, and the gallery shows
  only the canonical row.
* **One active project per team per event**, as a partial unique index over rows where
  `duplicate_of IS NULL`. That is what makes "keep both rows" compatible with "a team has one
  project".
"""

from __future__ import annotations

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q

from core import clock
from projects.validators import validate_web_url

TAGLINE_MAX_LENGTH = 140


class ProjectStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"


class ProjectQuerySet(models.QuerySet):
    def submitted(self):
        return self.filter(status=ProjectStatus.SUBMITTED)

    def canonical(self):
        """Excludes rows flagged as duplicates of an earlier submission."""
        return self.filter(duplicate_of__isnull=True)

    def gallery_visible(self):
        """The four conditions a project must meet to be shown publicly.

        Defined once, here, so the HTML gallery, the JSON API and every test are provably
        asking the same question. A second copy of this filter is how a draft eventually leaks.
        """
        return self.filter(
            status=ProjectStatus.SUBMITTED,
            duplicate_of__isnull=True,
            hidden_by_organizer=False,
            event__gallery_public=True,
        )


class Project(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="projects")
    team = models.ForeignKey("teams.Team", on_delete=models.CASCADE, related_name="projects")
    track = models.ForeignKey(
        "events.Track",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="projects",
        help_text="Optional while a draft; required to submit.",
    )

    name = models.CharField(max_length=200)
    tagline = models.CharField(
        max_length=TAGLINE_MAX_LENGTH,
        blank=True,
        validators=[MaxLengthValidator(TAGLINE_MAX_LENGTH)],
        help_text="One line, at most 140 characters.",
    )
    description = models.TextField(
        blank=True,
        help_text="Markdown. Rendered through markdown-it-py and sanitized with nh3.",
    )

    thumbnail = models.ImageField(
        upload_to="thumbnails/",
        null=True,
        blank=True,
        help_text="Verified with Pillow on upload; never trusted by file extension.",
    )
    # Recorded at upload from the format Pillow actually decoded, so the media view can send a
    # Content-Type without sniffing the bytes or trusting the filename at serve time.
    thumbnail_content_type = models.CharField(max_length=40, blank=True, editable=False)

    # http/https only, and rendered as links rather than embeds. An iframe for a video would
    # need an external host at view time, which the offline rule forbids, and would widen the
    # XSS surface for no benefit.
    demo_video_url = models.URLField(blank=True, validators=[validate_web_url])
    repo_url = models.URLField(blank=True, validators=[validate_web_url])
    live_url = models.URLField(blank=True, validators=[validate_web_url])

    status = models.CharField(
        max_length=20, choices=ProjectStatus.choices, default=ProjectStatus.DRAFT
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set once, when the project is first submitted. Later edits do not change it.",
    )

    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicates",
        help_text="Points at the earlier, canonical submission. Null for canonical rows.",
    )
    hidden_by_organizer = models.BooleanField(
        default=False,
        help_text="Organizer moderation. Hides the project from the gallery without deleting it.",
    )

    # Maintained by projects.services on every write and by the importer -- never by a database
    # trigger. A trigger would be invisible to the test suite and would need raw SQL in a
    # migration to defend; a service-layer recompute is one readable function that tests can
    # call directly.
    search_vector = SearchVectorField(null=True, editable=False)

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ["-submitted_at", "-created_at"]
        constraints = [
            # One live project per team per event. Scoped to canonical rows so that a flagged
            # duplicate can coexist with the submission it duplicates.
            models.UniqueConstraint(
                fields=["event", "team"],
                condition=Q(duplicate_of__isnull=True),
                name="project_one_active_per_team_per_event",
            ),
            # A submitted project has a submission time; a draft does not. Keeps "is it
            # submitted?" answerable from either column without them ever disagreeing.
            models.CheckConstraint(
                condition=Q(status=ProjectStatus.SUBMITTED, submitted_at__isnull=False)
                | Q(status=ProjectStatus.DRAFT, submitted_at__isnull=True),
                name="project_submitted_at_matches_status",
            ),
        ]
        indexes = [
            models.Index(fields=["event", "status"], name="project_event_status_idx"),
            models.Index(fields=["event", "track"], name="project_event_track_idx"),
            GinIndex(fields=["search_vector"], name="project_search_vector_gin"),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if self.duplicate_of_id and self.duplicate_of_id == self.pk:
            raise ValidationError({"duplicate_of": "A project cannot duplicate itself."})
        if self.team_id and self.event_id and self.team.event_id != self.event_id:
            raise ValidationError({"team": "The team belongs to a different event."})
        if self.track_id and self.event_id and self.track.event_id != self.event_id:
            raise ValidationError({"track": "The track belongs to a different event."})

    @property
    def is_submitted(self) -> bool:
        return self.status == ProjectStatus.SUBMITTED

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of_id is not None

    def missing_requirements(self) -> list[str]:
        """Field labels still missing before this project can be submitted.

        A draft needs only a name. Submitting needs the fields below plus every required custom
        question, which `projects.services` adds because it needs a database query.
        """
        missing = []
        if not self.name.strip():
            missing.append("name")
        if not self.tagline.strip():
            missing.append("tagline")
        if not self.description.strip():
            missing.append("description")
        if not self.track_id:
            missing.append("track")
        if not self.repo_url.strip():
            missing.append("repository URL")
        return missing


class ProjectImage(models.Model):
    """A gallery image. At most `settings.MAX_PROJECT_IMAGES` per project, enforced in services.

    The count limit is not a database constraint because "at most N rows per parent" is not
    expressible as one; the service layer checks it inside the same transaction as the insert.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="projects/")
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        help_text="Described for screen readers. Empty means decorative.",
    )
    order = models.PositiveSmallIntegerField(default=0)
    # As above: the decoded format, recorded once, so serving never has to guess.
    content_type = models.CharField(max_length=40, blank=True, editable=False)

    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["project", "order", "id"]

    def __str__(self) -> str:
        return f"image {self.order} of {self.project.name}"


class Tag(models.Model):
    """A normalized technology tag, shared across projects and events.

    Normalization (lowercase, trimmed, internal whitespace collapsed) happens in
    `projects.services.normalize_tag` before the row is looked up, so `React`, ` react ` and
    `REACT` are one tag rather than three near-duplicates in a filter dropdown.
    """

    name = models.CharField(max_length=60, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ProjectTag(models.Model):
    """Explicit through model for the project/tag relation.

    Explicit rather than a plain `ManyToManyField` so the join row can carry `created_at` and so
    a future feature (who added the tag, tag provenance from an import) has somewhere to live
    without a migration that rewrites the relation.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="project_tags")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="project_tags")
    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["project", "tag"]
        constraints = [
            models.UniqueConstraint(fields=["project", "tag"], name="projecttag_unique_pair"),
        ]

    def __str__(self) -> str:
        return f"{self.project.name} #{self.tag.name}"


class CustomAnswer(models.Model):
    """A project's answer to one of the event's custom questions."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(
        "events.CustomQuestion", on_delete=models.CASCADE, related_name="answers"
    )
    # One text column for every question kind. Booleans are stored as "true"/"false" and choices
    # as the chosen option, which keeps the schema flat and the validation in one place
    # (`projects.services`) rather than spread across five nullable typed columns that would
    # each need their own constraint.
    value = models.TextField(blank=True)

    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "question__order"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "question"], name="customanswer_unique_per_question"
            ),
        ]

    def __str__(self) -> str:
        return f"answer to {self.question_id} for {self.project_id}"

    def clean(self):
        if self.project_id and self.question_id and self.project.event_id != self.question.event_id:
            raise ValidationError(
                {"question": "The question belongs to a different event than the project."}
            )
