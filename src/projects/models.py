"""Projects (submissions), their images, tags and answers to the event's custom questions.

A project belongs to exactly one team, and a team has at most one project. It is either a
*draft* or *submitted*. Both can be edited until the deadline; only submitted projects reach the
gallery and judging.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from events.models import CustomQuestion, Event, Track
from teams.models import Team


class Status(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"


def image_path(instance, filename):
    # Random names: an upload's original filename never reaches the disk or a URL.
    ext = filename.rsplit(".", 1)[-1].lower()
    return f"projects/{secrets.token_hex(12)}.{ext}"


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Project(models.Model):
    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name="project")
    # Copied from team.event, so project queries per event need no join.
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="projects")

    name = models.CharField(max_length=120)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True, help_text="Markdown.")
    thumbnail = models.ImageField(upload_to=image_path, blank=True)
    demo_video_url = models.URLField(blank=True)
    repo_url = models.URLField(blank=True)
    live_url = models.URLField(blank=True)
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.PROTECT, related_name="projects"
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="projects")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            # submitted <=> has a submission time
            models.CheckConstraint(
                condition=(
                    Q(status=Status.DRAFT, submitted_at__isnull=True)
                    | Q(status=Status.SUBMITTED, submitted_at__isnull=False)
                ),
                name="project_status_matches_submitted_at",
            ),
        ]
        indexes = [models.Index(fields=["event", "status"], name="project_event_status_idx")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.event_id = self.team.event_id
        super().save(*args, **kwargs)

    @property
    def is_submitted(self):
        return self.status == Status.SUBMITTED

    def shown_answers(self):
        """Answers worth displaying: non-empty, to questions the organizer has not hidden."""
        return [
            a for a in self.answers.select_related("question").order_by("question__order", "question_id")
            if a.value and not a.question.is_hidden
        ]


class ProjectImage(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=image_path)
    caption = models.CharField(max_length=140, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["order", "id"]


class Answer(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="answers")
    # PROTECT: an answered question can be hidden, never deleted out from under its answers.
    question = models.ForeignKey(CustomQuestion, on_delete=models.PROTECT, related_name="answers")
    value = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "question"], name="one_answer_per_question"),
        ]
