"""Events and what an organizer configures on them: tracks, prizes, custom questions, and who
co-organizes.

All times are stored and shown in UTC. The event's *phase* (upcoming / open / judging /
finished) is never stored; it is computed from the dates, so it cannot drift out of sync with
them. The only stored state is `is_published`, which an organizer sets deliberately.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Phase(models.TextChoices):
    UPCOMING = "upcoming", "Upcoming"
    OPEN = "open", "Submissions open"
    JUDGING = "judging", "Judging"
    FINISHED = "finished", "Finished"


class EventQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_published=True)

    def managed_by(self, user):
        """Events `user` may configure: every event for an admin, their own for an organizer."""
        from accounts.roles import Role

        if not user.is_authenticated:
            return self.none()
        if user.role == Role.ADMIN:
            return self.all()
        if user.role == Role.ORGANIZER:
            return self.filter(organizer_links__user=user).distinct()
        return self.none()


class Event(models.Model):
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True, help_text="Markdown.")

    starts_at = models.DateTimeField()
    submissions_open_at = models.DateTimeField()
    submissions_close_at = models.DateTimeField()
    # The close time as first set, kept when an organizer extends the deadline for everyone,
    # so the pages can say "extended from ... to ...".
    original_submissions_close_at = models.DateTimeField(null=True, blank=True)
    judging_ends_at = models.DateTimeField()

    min_team_size = models.PositiveSmallIntegerField(
        default=1, help_text="A team needs at least this many members to submit."
    )
    max_team_size = models.PositiveSmallIntegerField(default=4)
    is_published = models.BooleanField(default=False)

    organizers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="EventOrganizer", through_fields=("event", "user"),
        related_name="organized_events",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-submissions_close_at"]
        constraints = [
            # The dates must describe a sensible timeline; the database refuses anything else.
            models.CheckConstraint(
                condition=Q(submissions_open_at__lt=F("submissions_close_at")),
                name="event_submissions_window_valid",
            ),
            models.CheckConstraint(
                condition=Q(submissions_close_at__lte=F("judging_ends_at")),
                name="event_judging_after_submissions",
            ),
            models.CheckConstraint(
                condition=Q(starts_at__lte=F("submissions_close_at")),
                name="event_starts_before_close",
            ),
            models.CheckConstraint(
                condition=Q(min_team_size__gte=1, max_team_size__lte=20)
                & Q(min_team_size__lte=F("max_team_size")),
                name="event_team_size_range",
            ),
        ]

    def __str__(self):
        return self.name

    def phase_at(self, now):
        if now < self.submissions_open_at:
            return Phase.UPCOMING
        if now < self.submissions_close_at:
            return Phase.OPEN
        if now < self.judging_ends_at:
            return Phase.JUDGING
        return Phase.FINISHED

    @property
    def phase(self):
        return self.phase_at(timezone.now())

    def get_phase_display(self):
        return Phase(self.phase).label

    @property
    def team_size_display(self):
        if self.min_team_size == self.max_team_size:
            return f"exactly {self.max_team_size}"
        if self.min_team_size == 1:
            return f"up to {self.max_team_size}"
        return f"{self.min_team_size} to {self.max_team_size}"

    def timeline(self):
        """[(label, when, passed)] for the four dates, in order."""
        now = timezone.now()
        return [
            (label, when, when <= now)
            for label, when in (
                ("event starts", self.starts_at),
                ("submissions open", self.submissions_open_at),
                ("submissions close", self.submissions_close_at),
                ("judging ends", self.judging_ends_at),
            )
        ]

    def visible_tracks(self):
        return self.tracks.filter(is_hidden=False)

    def visible_questions(self):
        return self.questions.filter(is_hidden=False)


class EventOrganizer(models.Model):
    """Who may configure an event. The creator is added automatically."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="organizer_links")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organizer_links"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["added_at"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="event_organizer_unique"),
        ]


class Track(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=300, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    # A track that projects already use can be hidden but not deleted (see services).
    is_hidden = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "name"], name="track_name_unique_per_event"),
        ]

    def __str__(self):
        return self.name


class Prize(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    title = models.CharField(max_length=120)
    value = models.CharField(max_length=60, blank=True, help_text='e.g. "$800" or "Swag box"')
    rank = models.PositiveSmallIntegerField(default=1, help_text="1 = first place")
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="prizes",
        help_text="Leave empty for an overall prize.",
    )
    description = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["rank", "id"]

    def __str__(self):
        return self.title


class QuestionKind(models.TextChoices):
    SHORT = "short", "Short text"
    LONG = "long", "Long text"
    URL = "url", "Link"
    CHOICE = "choice", "Single choice"
    CHECKBOX = "checkbox", "Checkbox (yes/no)"


class CustomQuestion(models.Model):
    """An organizer-defined question every submission in the event answers."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    prompt = models.CharField(max_length=200)
    help_text = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=10, choices=QuestionKind.choices, default=QuestionKind.SHORT)
    choices = models.TextField(blank=True, help_text="Single choice only: one option per line.")
    required = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)
    is_hidden = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.prompt

    def choice_list(self):
        return [line.strip() for line in self.choices.splitlines() if line.strip()]
