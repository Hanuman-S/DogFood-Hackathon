"""The audit log: one append-only table an organizer can read without a database client.

Auth writes to it now; later modules (submissions, judging, voting) add their own actions to
`AuditAction` and call `core.audit.record()` the same way.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditAction(models.TextChoices):
    # accounts
    SIGNUP = "signup", "Signed up"
    LOGIN_OK = "login_ok", "Logged in"
    LOGIN_FAILED = "login_failed", "Login failed"
    LOGIN_THROTTLED = "login_throttled", "Login refused (throttled)"
    LOGOUT = "logout", "Logged out"
    PASSWORD_CHANGED = "password_changed", "Changed password"
    SESSION_REVOKED = "session_revoked", "Revoked a session"
    SESSIONS_REVOKED_OTHERS = "sessions_revoked_others", "Signed out other sessions"
    TOKEN_CREATED = "token_created", "Created an API token"
    TOKEN_REVOKED = "token_revoked", "Revoked an API token"
    TOKEN_REJECTED = "token_rejected", "Rejected an invalid API token"
    ACCESS_DENIED = "access_denied", "Refused access to a portal"
    ACCOUNT_CREATED = "account_created", "Created an account for someone else"
    # events
    EVENT_CREATED = "event_created", "Created an event"
    EVENT_UPDATED = "event_updated", "Edited event settings"
    EVENT_PUBLISHED = "event_published", "Published an event"
    EVENT_UNPUBLISHED = "event_unpublished", "Unpublished an event"
    EVENT_PART_CHANGED = "event_part_changed", "Changed a track, prize or question"
    ORGANIZER_ADDED = "organizer_added", "Added a co-organizer"
    ORGANIZER_REMOVED = "organizer_removed", "Removed a co-organizer"
    # teams
    TEAM_CREATED = "team_created", "Created a team"
    TEAM_JOINED = "team_joined", "Joined a team"
    TEAM_JOIN_REFUSED = "team_join_refused", "Refused a team join"
    TEAM_LEFT = "team_left", "Left a team"
    TEAM_MEMBER_REMOVED = "team_member_removed", "Removed a team member"
    TEAM_CAPTAIN_CHANGED = "team_captain_changed", "Handed over captaincy"
    TEAM_RENAMED = "team_renamed", "Renamed a team"
    TEAM_LINK_RESET = "team_link_reset", "Replaced a team's invite link"
    TEAM_DISBANDED = "team_disbanded", "Disbanded a team"
    # projects
    PROJECT_CREATED = "project_created", "Started a project"
    PROJECT_UPDATED = "project_updated", "Edited a project"
    PROJECT_SUBMITTED = "project_submitted", "Submitted a project"
    PROJECT_UNSUBMITTED = "project_unsubmitted", "Withdrew a submission to draft"
    PROJECT_IMAGE_ADDED = "project_image_added", "Added a project image"
    PROJECT_IMAGE_REMOVED = "project_image_removed", "Removed a project image"
    # deadlines
    LATE_WRITE_REFUSED = "late_write_refused", "Refused a write after the deadline"
    EARLY_WRITE_REFUSED = "early_write_refused", "Refused a write before submissions opened"
    DEADLINE_CHANGED = "deadline_changed", "Changed the submission deadline"
    DEADLINE_EXTENDED = "deadline_extended", "Extended the deadline for everyone"
    TEAM_EXTENSION_GRANTED = "team_extension_granted", "Granted a team an extension"
    TEAM_EXTENSION_REVOKED = "team_extension_revoked", "Revoked a team's extension"
    DEADLINE_BYPASSED = "deadline_bypassed", "Wrote past the deadline (organizer bypass)"
    # imports
    FIXTURES_IMPORTED = "fixtures_imported", "Imported the fixture dataset"


class AuditLog(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    # SET_NULL + an email snapshot: the trail must outlive the account it describes.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=40, choices=AuditAction.choices)
    # What the action was about, as text: the email a login attempted, a portal name, a token
    # prefix. Indexed, because the login throttle counts rows by (action, subject, ip).
    subject = models.CharField(max_length=254, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["action", "subject", "created_at"], name="audit_throttle_idx"),
            models.Index(fields=["action", "ip", "created_at"], name="audit_ip_idx"),
        ]

    def __str__(self):
        who = self.actor_email or "anonymous"
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {who} {self.action} {self.subject}"
