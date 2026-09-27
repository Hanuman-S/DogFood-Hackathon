"""Refusals from the voting services, each with the HTTP status and error code every caller
answers with (core.api.error(status, code, detail)), so the pages and the JSON API refuse the same
thing the same way."""


class VotingError(Exception):
    status = 400
    code = "voting_error"

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail or self.code)


class NoVoting(VotingError):
    status = 404
    code = "no_voting"


class VotingNotOpen(VotingError):
    status = 409
    code = "voting_not_open"


class VotingClosed(VotingError):
    status = 409
    code = "voting_closed"


class VotingOpen(VotingError):
    """Refused while voting has not closed yet (publishing final results)."""

    status = 409
    code = "voting_open"


class StaffCannotVote(VotingError):
    status = 403
    code = "staff_cannot_vote"


class AccountTooNew(VotingError):
    status = 403
    code = "account_too_new"


class OwnProject(VotingError):
    status = 403
    code = "own_project"


class OverBudget(VotingError):
    status = 400
    code = "over_budget"


class InvalidBallot(VotingError):
    status = 400
    code = "invalid_ballot"


class InvalidVotingConfig(VotingError):
    status = 400
    code = "invalid_voting_config"


class VotingConfigLocked(VotingError):
    status = 409
    code = "voting_config_locked"


class AccessModeUnavailable(VotingError):
    status = 400
    code = "access_mode_unavailable"
