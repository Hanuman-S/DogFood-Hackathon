"""Events and what an organizer configures on them: tracks, prizes, custom questions, and who
holds which role in them.

**Roles are per event** (`EventMembership`). A global "is a judge" column would be wrong: the
same person judges one hackathon and competes in the next, and an organizer's powers stop at the
edge of their own event. The conflict-of-interest rule -- nobody is both a competitor and staff
in one event -- is enforced by an exclusion constraint on the membership table (see
events/migrations/0002_membership_conflict_of_interest.py), not only by the service layer.

The timeline is strictly ordered, and the database refuses anything else:

    event starts < submissions open < submissions close < judging starts < judging ends
                                                                      < results (optional: TBD)

All times are stored and shown in UTC. The event's *phase* (upcoming / open / closed / judging /
finished) is never stored; it is computed from the dates, so it cannot drift out of sync with
them. The only stored state is `is_published`, which an organizer sets deliberately.
"""

from django.conf import settings
from django.db import models
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone

from accounts.roles import COMPETITOR_ROLES, Role


class Phase(models.TextChoices):
    UPCOMING = "upcoming", "Upcoming"
    OPEN = "open", "Submissions open"
    # Between the submission close and the judging start: nothing can be submitted, nothing is
    # being scored yet (the organizer assigns judges here).
    CLOSED = "closed", "Submissions closed"
    JUDGING = "judging", "Judging"
    FINISHED = "finished", "Finished"


class EventQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_published=True)

    def managed_by(self, user):
        """Events `user` may configure: every event for an admin, their own for an organizer."""
        if not user.is_authenticated:
            return self.none()
        if user.is_platform_admin:
            return self.all()
        return self.with_role(user, Role.ORGANIZER)

    def with_role(self, user, role):
        """Events in which `user` holds `role`."""
        if not user.is_authenticated:
            return self.none()
        return self.filter(memberships__user=user, memberships__role=role).distinct()


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
    judging_starts_at = models.DateTimeField()
    judging_ends_at = models.DateTimeField()
    # When results are announced. Empty = "to be announced"; it may change at any time, but once
    # set it must come after the judging end.
    results_at = models.DateTimeField(null=True, blank=True)
    # The judging end as first set, kept when an organizer extends judging (an audited action),
    # so the pages can say "extended from ... to ...".
    original_judging_ends_at = models.DateTimeField(null=True, blank=True)

    min_team_size = models.PositiveSmallIntegerField(
        default=1, help_text="A team needs at least this many members to submit."
    )
    max_team_size = models.PositiveSmallIntegerField(default=4)
    is_published = models.BooleanField(default=False)

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
            # Each date strictly after the one before it:
            # starts < open < close < judging starts < judging ends < results (when set).
            models.CheckConstraint(
                condition=Q(starts_at__lt=F("submissions_open_at")),
                name="event_starts_before_submissions_open",
            ),
            models.CheckConstraint(
                condition=Q(submissions_open_at__lt=F("submissions_close_at")),
                name="event_submissions_window_valid",
            ),
            models.CheckConstraint(
                condition=Q(submissions_close_at__lt=F("judging_starts_at")),
                name="event_judging_starts_after_close",
            ),
            models.CheckConstraint(
                condition=Q(judging_starts_at__lt=F("judging_ends_at")),
                name="event_judging_window_valid",
            ),
            models.CheckConstraint(
                condition=Q(results_at__isnull=True) | Q(judging_ends_at__lt=F("results_at")),
                name="event_results_after_judging",
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
        if now < self.judging_starts_at:
            return Phase.CLOSED
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
        """[(label, when, passed)] for every date, in order. `when` is None for results TBD."""
        now = timezone.now()
        return [
            (label, when, when is not None and when <= now)
            for label, when in (
                ("event starts", self.starts_at),
                ("submissions open", self.submissions_open_at),
                ("submissions close", self.submissions_close_at),
                ("judging starts", self.judging_starts_at),
                ("judging ends", self.judging_ends_at),
                ("results", self.results_at),
            )
        ]

    def visible_tracks(self):
        return self.tracks.filter(is_hidden=False)

    def visible_questions(self):
        return self.questions.filter(is_hidden=False)


    def members_with(self, role):
        """The users holding `role` in this event."""
        from accounts.models import User

        return User.objects.filter(event_memberships__event=self, event_memberships__role=role)


SIDE_COMPETITOR = "competitor"
SIDE_STAFF = "staff"


class EventMembership(models.Model):
    """A user's role in one event. One person may hold several roles, within limits.

    Allowed: judge + organizer (a small hackathon's organizer often judges too).
    Forbidden: participant + judge, or participant + organizer -- the conflict-of-interest rule.

    A participant membership exists exactly while the person is on a team in the event: forming
    or joining a team registers you, leaving it unregisters you (teams/services.py).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_memberships"
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    # Which side of the conflict-of-interest line this role sits on. A stored generated column
    # (computed by the database, not by application code) so that the exclusion constraint can
    # compare sides without trusting anything Python wrote.
    side = models.GeneratedField(
        expression=Case(
            When(role__in=[r.value for r in COMPETITOR_ROLES], then=Value(SIDE_COMPETITOR)),
            default=Value(SIDE_STAFF),
        ),
        output_field=models.CharField(max_length=16),
        db_persist=True,
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["event", "role", "added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event", "role"], name="membership_unique_user_event_role"
            ),
            models.CheckConstraint(
                condition=Q(role__in=Role.values), name="membership_role_valid"
            ),
        ]

    def __str__(self):
        return f"{self.user.email} as {self.role} in {self.event.slug}"


class JudgeTrack(models.Model):
    """Which tracks a judge covers. Imported from the fixture now; T2 builds assignments on it."""

    membership = models.ForeignKey(
        EventMembership, on_delete=models.CASCADE, related_name="judge_tracks"
    )
    track = models.ForeignKey("Track", on_delete=models.CASCADE, related_name="judge_tracks")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["membership", "track"], name="judge_track_unique"),
        ]


class JudgeInviteQuerySet(models.QuerySet):
    def open(self, now=None):
        """Invites that can still be accepted."""
        now = now or timezone.now()
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now)


class JudgeInvite(models.Model):
    """A one-time link that makes whoever opens it (and signs up or logs in as `email`) a judge
    of `event`, covering `tracks` (none = every track).

    Only a SHA-256 digest of the token is stored, like API tokens: the raw link is shown to the
    organizer once. Accepting, revoking and expiry are timestamps, never deletes, so the audit
    trail can always say who was invited, by whom, and what became of it.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="judge_invites")
    email = models.EmailField(max_length=254)
    tracks = models.ManyToManyField("Track", blank=True, related_name="+")
    digest = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = JudgeInviteQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One pending invite per person per event: re-inviting revokes the old link first.
            models.UniqueConstraint(
                fields=["event", "email"],
                condition=Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="judge_invite_one_pending_per_email",
            ),
            models.CheckConstraint(
                condition=Q(accepted_at__isnull=True) | Q(revoked_at__isnull=True),
                name="judge_invite_not_accepted_and_revoked",
            ),
        ]

    def __str__(self):
        return f"judge invite for {self.email} to {self.event.slug}"


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
