"""The audit log: one append-only table an organizer can read without a database client.

Every module adds its actions to `AuditAction` and writes through `core.audit.record()`.
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
    JUDGE_ADDED = "judge_added", "Made someone a judge of an event"
    JUDGE_REMOVED = "judge_removed", "Removed a judge from an event"
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
    # judging (T2). All three T2 parts add their actions here, in one place, so they share one
    # migration instead of three conflicting ones.
    # -- organizer: rubric, judges, window, assignment
    CRITERION_ADDED = "criterion_added", "Added a rubric criterion"
    CRITERION_UPDATED = "criterion_updated", "Edited a rubric criterion"
    CRITERION_REMOVED = "criterion_removed", "Removed a rubric criterion"
    RUBRIC_CHANGE_REFUSED = "rubric_change_refused", "Refused a rubric change (rubric locked)"
    JUDGE_INVITED = "judge_invited", "Created a judge invite link"
    JUDGE_INVITE_ACCEPTED = "judge_invite_accepted", "Accepted a judge invite"
    JUDGE_INVITE_REVOKED = "judge_invite_revoked", "Revoked a judge invite"
    JUDGE_INVITE_REFUSED = "judge_invite_refused", "Refused a judge invite"
    JUDGING_EXTENDED = "judging_extended", "Extended judging"
    JUDGING_EXTENSION_REFUSED = "judging_extension_refused", "Refused to extend judging (a final result exists)"
    JUDGING_ENDED_EARLY = "judging_ended_early", "Ended judging now"
    JUDGING_END_REFUSED = "judging_end_refused", "Refused to end judging now"
    ASSIGNMENTS_GENERATED = "assignments_generated", "Ran an automatic assignment round"
    ASSIGNMENT_ADDED = "assignment_added", "Assigned a project to a judge"
    ASSIGNMENT_WITHDRAWN = "assignment_withdrawn", "Withdrew an assignment"
    ASSIGNMENT_MOVED = "assignment_moved", "Moved an assignment to another judge"
    ASSIGNMENT_DECLINED = "assignment_declined", "Declined an assignment (conflict of interest)"
    JUDGE_NUDGED = "judge_nudged", "Reminded a judge"
    # -- judge: reviews
    SCORE_SAVED = "score_saved", "Saved a review draft"
    SCORE_SUBMITTED = "score_submitted", "Submitted a review"
    SCORE_REOPENED = "score_reopened", "Reopened a submitted review"
    JUDGING_WRITE_REFUSED = "judging_write_refused", "Refused a review outside the judging window"
    # -- scoring engine: results
    RESULTS_COMPUTED = "results_computed", "Computed results"
    RESULTS_PUBLISHED = "results_published", "Published results"
    JUDGE_EXCLUDED = "judge_excluded", "Excluded a judge's reviews from results"
    REVIEW_FLAG_RESOLVED = "review_flag_resolved", "Resolved a flagged review"
    SNAPSHOT_CREATED = "snapshot_created", "Computed a results snapshot"
    SNAPSHOT_REFUSED = "snapshot_refused", "Refused to compute a results snapshot"
    SCORING_CONFIG_CHANGED = "scoring_config_changed", "Changed an event's scoring configuration"
    SCORING_CONFIG_REFUSED = "scoring_config_refused", "Refused a change to an event's scoring configuration"
    EXPORT_DOWNLOADED = "export_downloaded", "Downloaded a CSV export"
    # results (T2 completion): publishing, visibility, the winners download
    RESULTS_UNPUBLISHED = "results_unpublished", "Unpublished an event's results"
    RESULTS_PUBLISH_REFUSED = "results_publish_refused", "Refused to publish or unpublish results"
    RESULT_SETTINGS_CHANGED = "result_settings_changed", "Changed who can see an event's results"
    WINNERS_EXPORTED = "winners_exported", "Downloaded the winners and their contacts"
    # community voting (T3)
    EVENT_CHANGE_REFUSED = "event_change_refused", "Refused a date change (community voting is scheduled)"
    VOTING_CONFIG_CHANGED = "voting_config_changed", "Set up or changed an event's community vote"
    VOTING_CONFIG_REFUSED = "voting_config_refused", "Refused a change to an event's community vote"
    VOTING_REMOVED = "voting_removed", "Removed an event's community vote before it opened"
    VOTING_ENDED_EARLY = "voting_ended_early", "Ended voting now"
    VOTING_END_REFUSED = "voting_end_refused", "Refused to end voting now"
    BALLOT_OPENED = "ballot_opened", "Opened a ballot"
    VOTE_CAST = "vote_cast", "Cast a vote"
    VOTE_CHANGED = "vote_changed", "Changed a vote"
    VOTE_REFUSED = "vote_refused", "Refused a vote"
    VOTE_LATE_REFUSED = "vote_late_refused", "Refused a vote outside the voting window"
    TALLY_EXPORTED = "tally_exported", "Downloaded a vote tally"
    VOTER_LINKS_ADDED = "voter_links_added", "Allowlisted voter emails"
    VOTER_LINK_REVOKED = "voter_link_revoked", "Revoked a voter link"
    VOTER_LINKS_EXPORTED = "voter_links_exported", "Downloaded voter links"
    OPEN_LINK_ROTATED = "open_link_rotated", "Replaced an event's open voting link"
    VOTING_BYPASSED = "voting_bypassed", "Wrote to ballots past the voting trigger (bypass)"


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
