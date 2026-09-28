"""Refusals from the comment services, each with the HTTP status and error code every caller answers
with (core.api.error(status, code, detail)), so the pages and the JSON API refuse the same thing the
same way."""


class CommentError(Exception):
    status = 400
    code = "comment_error"

    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail or self.code)


class LoginRequired(CommentError):
    status = 401
    code = "login_required"


class NotCommentable(CommentError):
    """No such project in the public gallery (a draft, an unpublished event, or no project at all)."""

    status = 404
    code = "no_project"


class NoComment(CommentError):
    """No such comment, or not one this caller may act on: the same answer either way, so comment ids
    cannot be probed."""

    status = 404
    code = "no_comment"


class NoEvent(CommentError):
    status = 404
    code = "no_event"


class CommentsDisabled(CommentError):
    status = 409
    code = "comments_disabled"


class DuplicateComment(CommentError):
    status = 409
    code = "duplicate_comment"


class RateLimited(CommentError):
    status = 429
    code = "rate_limited"


class InvalidComment(CommentError):
    status = 400
    code = "invalid_comment"


class InvalidModeration(CommentError):
    status = 400
    code = "invalid_moderation"
