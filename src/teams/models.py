"""Teams, formed by invite link.

Rules, and where each one is enforced:

| Rule                                           | Enforced by                                   |
| ---------------------------------------------- | --------------------------------------------- |
| one team per participant per event             | DB unique constraint on (event, user)         |
| one captain per team, and they are a member    | captain FK + services (captain set on join)   |
| team size <= event.max_team_size               | services, under a row lock on the team        |
| only *participants* can be on a team           | services (role check)                         |
| team name unique within an event (any case)    | DB unique constraint on (event, lower(name))  |

The participant-only rule follows from the one-role-per-account model: judges, organizers and
admins can never compete, so conflict of interest is impossible by construction. If roles ever
become per-event (see ARCHITECTURE.md), this rule moves to "not staff *in this event*" and is
the one place in teams/services.py to change.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from events.models import Event


def new_invite_token():
    return secrets.token_urlsafe(16)


class Team(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=80)
    captain = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="captained_teams"
    )
    # One reusable link per team. Resetting it replaces the token, which kills the old link.
    invite_token = models.CharField(max_length=40, unique=True, default=new_invite_token)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("event", Lower("name"), name="team_name_unique_per_event"),
        ]

    def __str__(self):
        return self.name

    @property
    def size(self):
        return self.members.count()

    def is_full(self):
        return self.size >= self.event.max_team_size


class TeamMember(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships"
    )
    # Copied from team.event so the database itself can refuse a second team in one event.
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["joined_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="one_team_per_event"),
        ]

    def save(self, *args, **kwargs):
        self.event_id = self.team.event_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} in {self.team}"


class TeamExtension(models.Model):
    """Extra time for one team, past the event's close (e.g. their upload failed at 23:28).

    The database trigger that enforces deadlines reads this table too, so an extension is
    honoured everywhere the deadline is -- and nowhere else.
    """

    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name="extension")
    until = models.DateTimeField()
    reason = models.CharField(max_length=200)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    granted_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.team} until {self.until:%Y-%m-%d %H:%M} UTC"
