"""Refusals from the scoring services. Each carries the HTTP status and error code a view or the
JSON API should answer with (core.api.error(status, code, detail)), so every caller refuses the
same thing the same way. Permission refusals use Django's PermissionDenied (403)."""


class ScoringError(Exception):
    status = 400
    code = "scoring_error"

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail or self.code)


class JudgingOpen(ScoringError):
    """A final snapshot was asked for before judging closed."""

    status = 409
    code = "judging_open"


class ScoringConfigLocked(ScoringError):
    """The event's scoring configuration cannot change once judging has closed."""

    status = 409
    code = "scoring_config_locked"


class FinalOverrideRefused(ScoringError):
    """A final snapshot always uses the event's own configuration and weights."""

    status = 400
    code = "final_uses_event_config"


class InvalidConfig(ScoringError):
    status = 400
    code = "invalid_config"


class SnapshotInsideTransaction(ScoringError):
    """compute_snapshot must open its own REPEATABLE READ transaction; it cannot join another.

    A programming error, not a user refusal: a view that calls compute_snapshot must be marked
    @transaction.non_atomic_requests (and ATOMIC_REQUESTS is off in this project)."""

    status = 500
    code = "snapshot_inside_transaction"


# --- publishing results -----------------------------------------------------------------------------

class NoSuchSnapshot(ScoringError):
    status = 404
    code = "no_such_snapshot"


class NotFinal(ScoringError):
    """Only a final snapshot can be published."""

    status = 400
    code = "not_final"


class NotLatestFinal(ScoringError):
    """A newer final exists: publishing an older one would publish a result the organizers have
    already replaced."""

    status = 409
    code = "not_latest_final"


class AlreadyPublished(ScoringError):
    status = 409
    code = "already_published"


class NotPublished(ScoringError):
    status = 409
    code = "not_published"


class NoFinalResult(ScoringError):
    """Winners are named from a final result only."""

    status = 409
    code = "no_final_result"


class InvalidResultSettings(ScoringError):
    status = 400
    code = "invalid_result_settings"
