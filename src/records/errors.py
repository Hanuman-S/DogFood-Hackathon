"""Refusals from the record services, each with the HTTP status and code every caller answers with."""


class RecordError(Exception):
    status = 400
    code = "record_error"

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail or self.code)


class NoEvent(RecordError):
    status = 404
    code = "no_event"


class NoRecord(RecordError):
    status = 404
    code = "no_record"


class InvalidKind(RecordError):
    status = 400
    code = "invalid_kind"


class InvalidRevoke(RecordError):
    status = 400
    code = "invalid_revoke"


class JudgingOpen(RecordError):
    status = 409
    code = "judging_open"


class SubmissionsOpen(RecordError):
    status = 409
    code = "submissions_open"


class NoPublishedFinal(RecordError):
    status = 409
    code = "no_published_final"


class AlreadyRevoked(RecordError):
    status = 409
    code = "already_revoked"


class RateLimited(RecordError):
    status = 429
    code = "rate_limited"


class SigningUnavailable(RecordError):
    status = 503
    code = "signing_unavailable"
