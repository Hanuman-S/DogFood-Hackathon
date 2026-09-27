"""Community voting (T3): one event's voting window and rules, and the voters' ballots.

Writes go only through `voting/services.py`. The window is enforced twice, like the submission
deadline: the service checks it first, and a Postgres trigger (voting/migrations/0002) refuses any
INSERT or UPDATE of a ballot or ballot line outside [opens_at, closes_at) by the database clock.
The one exception is voiding: an UPDATE of a ballot that changes only its void fields is allowed
at any time, so an organizer can void a ballot after voting has closed. DELETE of a ballot or a
line is refused once voting has opened (voting/migrations/0004), and the foreign keys into these
tables from events, projects and accounts are PROTECT, so no cascade can remove a vote either.
`voting.services.voting_bypass` is the one, audited, way past the trigger.

Nobody but the event's organizers and platform admins reads a tally (`voting.services.tally`).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q

CREDIT_BUDGET_MAX = 1000


class AccessMode(models.TextChoices):
    AUTHENTICATED = "authenticated", "Logged-in accounts"
    EMAIL_GATED = "email_gated", "Email allowlist (one link per email)"
    OPEN_LINK = "open_link", "Open link (weakest: can be gamed)"


class Method(models.TextChoices):
    ONE_PERSON_ONE_VOTE = "one_person_one_vote", "One person, one vote"
    QUADRATIC = "quadratic", "Quadratic (influence = square root of credits)"


class VotingConfig(models.Model):
    """An event's community vote. No row = no voting.

    * opens_at >= the effective submission close (the event's close, or the latest team
      extension), so the set of projects on the ballot is fixed; opens_at < closes_at. It may
      overlap judging.
    * method: one_person_one_vote is a budget of 1; quadratic gives each voter `credit_budget`
      credits, and a project's influence from one ballot is sqrt(credits placed on it).
    * Once voting has opened, only closes_at can change (the rules a ballot was cast under stay
      put), and never past an active publication of the results.
    * `ballot_secret` seeds each ballot's project order (with the ballot id); it is never shown.
    """

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="voting")
    opens_at = models.DateTimeField()
    closes_at = models.DateTimeField()
    # The first close, when "end voting now" or an edit moved it; pages say what changed.
    original_closes_at = models.DateTimeField(null=True, blank=True)
    access_mode = models.CharField(max_length=20, choices=AccessMode.choices, default=AccessMode.AUTHENTICATED)
    method = models.CharField(max_length=24, choices=Method.choices, default=Method.QUADRATIC)
    credit_budget = models.PositiveSmallIntegerField(default=16)
    accounts_before_open_only = models.BooleanField(
        default=True,
        help_text="Refuse accounts created at or after voting opens (makes sign-up stuffing harder).",
    )
    ballot_secret = models.CharField(max_length=64, editable=False)
    # open_link mode: the event's one link is derived from this (voting.links); a new nonce is a new
    # link, and the old one stops working. Empty until the organizer first shows the link.
    open_link_nonce = models.CharField(max_length=32, blank=True, editable=False)
    # Bumped by every tally freeze, inside compute_snapshot's REPEATABLE READ transaction, with this
    # row locked: a second final computed at the same moment then fails to serialize (409, retry)
    # instead of freezing from a snapshot that cannot see the first one's tally.
    tally_freezes = models.PositiveIntegerField(default=0, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(opens_at__lt=F("closes_at")), name="voting_window_valid"),
            models.CheckConstraint(condition=Q(access_mode__in=AccessMode.values), name="voting_access_mode_valid"),
            models.CheckConstraint(condition=Q(method__in=Method.values), name="voting_method_valid"),
            models.CheckConstraint(
                condition=Q(credit_budget__gte=1, credit_budget__lte=CREDIT_BUDGET_MAX),
                name="voting_credit_budget_range",
            ),
            models.CheckConstraint(
                condition=~Q(method=Method.ONE_PERSON_ONE_VOTE) | Q(credit_budget=1),
                name="voting_one_person_one_vote_budget_is_1",
            ),
        ]

    def __str__(self):
        return f"voting for {self.event_id}"

    @property
    def budget(self):
        return 1 if self.method == Method.ONE_PERSON_ONE_VOTE else self.credit_budget


class VoterLink(models.Model):
    """email_gated mode: one ballot link per allowlisted email.

    The link's token is not stored. It is derived (voting.links.link_token: an HMAC keyed from
    SECRET_KEY over the link id and `nonce`), so the organizer can download voter-links.csv at any
    time; only a SHA-256 digest is stored, to find the link from a token. Revoking stops the link;
    re-adding a revoked email issues a new nonce, so the old link stays dead.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="voter_links")
    email = models.EmailField(max_length=254)
    nonce = models.CharField(max_length=32, editable=False)
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        ordering = ["event", "email"]
        constraints = [
            models.UniqueConstraint(fields=["event", "email"], name="voterlink_one_per_email_per_event"),
        ]

    def __str__(self):
        return f"voter link for {self.email}"


class Ballot(models.Model):
    """One voter's ballot in one event. The voter is identified by exactly one identity, matching
    the event's access mode: an account (`voter_user`), an email link (`voter_link`), or the random
    id in an open-link cookie (`voter_cookie`). Unique per identity per event.

    `ip_hash` is a keyed hash of the address of the last write (core.net.hash_ip); no IP is
    stored. A voided ballot is kept (with who, when and why) and left out of every tally.
    """

    # PROTECT: an event with ballots cannot be deleted (ballots are records; see the trigger).
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="ballots")
    voter_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="ballots"
    )
    voter_link = models.ForeignKey(VoterLink, null=True, blank=True, on_delete=models.PROTECT, related_name="ballots")
    # open_link mode: the random id in the voter's signed cookie.
    voter_cookie = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    ip_hash = models.CharField(max_length=64, blank=True)  # of the last write
    created_ip_hash = models.CharField(max_length=64, blank=True)  # of the write that created it
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    void_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["event", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "voter_user"], condition=Q(voter_user__isnull=False),
                name="ballot_one_per_user_per_event",
            ),
            models.UniqueConstraint(
                fields=["event", "voter_link"], condition=Q(voter_link__isnull=False),
                name="ballot_one_per_link_per_event",
            ),
            models.UniqueConstraint(
                fields=["event", "voter_cookie"], condition=~Q(voter_cookie=""),
                name="ballot_one_per_cookie_per_event",
            ),
            # Exactly one identity.
            models.CheckConstraint(
                condition=Q(voter_user__isnull=False, voter_link__isnull=True, voter_cookie="")
                | Q(voter_user__isnull=True, voter_link__isnull=False, voter_cookie="")
                | (Q(voter_user__isnull=True, voter_link__isnull=True) & ~Q(voter_cookie="")),
                name="ballot_has_exactly_one_voter",
            ),
            models.CheckConstraint(
                condition=Q(voided_at__isnull=True, voided_by__isnull=True)
                | Q(voided_at__isnull=False, voided_by__isnull=False),
                name="ballot_void_fields_together",
            ),
        ]

    def __str__(self):
        return f"ballot {self.pk} in {self.event_id}"


class BallotLine(models.Model):
    """The credits one ballot places on one project, and where that project was shown on this
    voter's ballot (the per-ballot shuffle), kept for the position-bias check."""

    ballot = models.ForeignKey(Ballot, on_delete=models.CASCADE, related_name="lines")
    # PROTECT: a project with ballot lines cannot be deleted, directly or through its team.
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="ballot_lines")
    credits = models.PositiveSmallIntegerField(default=0)
    shown_position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["ballot", "shown_position"]
        constraints = [
            models.UniqueConstraint(fields=["ballot", "project"], name="ballotline_one_per_project"),
            models.UniqueConstraint(fields=["ballot", "shown_position"], name="ballotline_one_per_position"),
            models.CheckConstraint(condition=Q(credits__lte=CREDIT_BUDGET_MAX), name="ballotline_credits_range"),
        ]

    def __str__(self):
        return f"{self.credits} on {self.project_id}"


class TallyImmutable(Exception):
    pass


class VoteTallySnapshot(models.Model):
    """One frozen count of an event's votes. **Immutable and append-only**, many per event.

    Frozen only by "Compute final results" (scoring.services.compute_snapshot, after voting has
    closed), never by a page view. `rows` is per submitted project: influence (the sum of sqrt of the
    credits each non-voided ballot placed on it), ballots and credits, in project order. A void or a
    restore after a freeze changes nothing here; it shows in the next freeze, and that freeze records
    `changed_since_previous` and which ballots were voided or restored in between.

    `save()` refuses an update and a Postgres trigger (voting/migrations/0008) refuses any UPDATE.
    """

    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="vote_tallies")
    created_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_by_email = models.CharField(max_length=254)
    method = models.CharField(max_length=24)
    budget = models.PositiveSmallIntegerField()
    rows = models.JSONField()
    counts = models.JSONField(default=dict)
    voided_ballot_ids = models.JSONField(default=list)
    input_hash = models.CharField(max_length=64)
    previous = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    changed_since_previous = models.BooleanField(default=False)
    voided_since_previous = models.JSONField(default=list)
    restored_since_previous = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"vote tally {self.pk} of {self.event_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TallyImmutable("A vote tally is immutable; freeze a new one instead.")
        super().save(*args, **kwargs)
