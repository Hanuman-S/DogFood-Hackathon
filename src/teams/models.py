"""Teams and invite links.

Two constraints here carry real weight:

* **One team per user per event**, enforced by a unique index on `(event, user)` rather than by
  a service-layer check alone. It is the rule most likely to be broken by a race -- two
  simultaneous invite redemptions -- and a check-then-insert cannot win that race. The database
  can.
* **One captain per team**, as a partial unique index. A team with two captains or none is a
  team where "the captain must transfer captaincy before leaving" has no meaning.

Team names are deliberately *not* unique. The organizer fixture contains `StillTrail` three
times, and rejecting that would be inventing a rule the data does not follow.
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core import clock

# 32 bytes from `secrets`, URL-safe base64 -> 43 characters. The brief asks for 32+ bytes.
INVITE_TOKEN_BYTES = 32
DEFAULT_INVITE_DAYS = 7


class Team(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="teams")
    # NOT unique: see the module docstring.
    name = models.CharField(max_length=200)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_teams",
    )

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "name"]
        indexes = [
            models.Index(fields=["event", "name"], name="team_event_name_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def member_count(self) -> int:
        return self.members.count()

    @property
    def is_full(self) -> bool:
        return self.member_count >= self.event.max_team_size

    def captain(self):
        return self.members.filter(is_captain=True).select_related("user").first()


class TeamMember(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships"
    )
    # Denormalized from `team.event`. It exists solely so the "one team per user per event"
    # rule can be a single-table unique index; deriving it through the join would make that
    # constraint impossible to express. `clean()` and the service layer keep it consistent, and
    # a test asserts they agree.
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, related_name="team_members"
    )
    is_captain = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=clock.now, editable=False)

    class Meta:
        ordering = ["team", "-is_captain", "user"]
        constraints = [
            # The rule that matters. A participant belongs to at most one team per event.
            models.UniqueConstraint(
                fields=["event", "user"], name="teammember_one_team_per_user_per_event"
            ),
            # Exactly one captain per team, by making a second captain row impossible.
            models.UniqueConstraint(
                fields=["team"],
                condition=Q(is_captain=True),
                name="teammember_single_captain_per_team",
            ),
        ]

    def __str__(self) -> str:
        suffix = " (captain)" if self.is_captain else ""
        return f"{self.user.email} in {self.team.name}{suffix}"

    def clean(self):
        if self.team_id and self.event_id and self.team.event_id != self.event_id:
            raise ValidationError(
                {"event": "The denormalized event must match the team's event."}
            )


class TeamInviteQuerySet(models.QuerySet):
    def live(self):
        """Invites that are neither revoked nor expired. Does not consider use count."""
        return self.filter(revoked_at__isnull=True, expires_at__gt=clock.now())


class TeamInvite(models.Model):
    """A shareable join link.

    There is no email delivery anywhere in this portal -- the "runs offline with no hosted
    services" rule forbids an SMTP provider -- so an invite is a URL the captain copies and
    sends however they like.

    Only the SHA-256 of the token is stored, exactly as for API tokens: the link is a bearer
    credential, and a leaked database should not hand over working invitations.
    """

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="invites")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_invites",
    )
    expires_at = models.DateTimeField()
    max_uses = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Null means unlimited uses before expiry."
    )
    use_count = models.PositiveIntegerField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=clock.now, editable=False)

    objects = TeamInviteQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(max_uses__isnull=True) | Q(max_uses__gte=1),
                name="teaminvite_max_uses_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"invite to {self.team.name} ({self.state})"

    # --- token handling -----------------------------------------------------------------

    @staticmethod
    def hash_token(plaintext: str) -> str:
        return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def generate_plaintext() -> str:
        return secrets.token_urlsafe(INVITE_TOKEN_BYTES)

    # --- state ---------------------------------------------------------------------------
    #
    # Each predicate is separate, and the service layer checks them one at a time, because the
    # brief requires a *specific* message for each refusal. A single `is_usable` boolean would
    # collapse "expired", "revoked" and "fully used" into one unhelpful "invalid link".

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return clock.now() >= self.expires_at

    @property
    def is_exhausted(self) -> bool:
        return self.max_uses is not None and self.use_count >= self.max_uses

    @property
    def state(self) -> str:
        if self.is_revoked:
            return "revoked"
        if self.is_expired:
            return "expired"
        if self.is_exhausted:
            return "exhausted"
        return "live"

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = clock.now()
            self.save(update_fields=["revoked_at"])
