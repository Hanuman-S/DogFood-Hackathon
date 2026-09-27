"""Portal error codes and the single place they become HTTP responses.

Every refusal the portal makes has a stable machine-readable `code`. The API returns it as
`{"error": "<code>", ...}` and the UI renders the same message in a banner, so a participant
who hits a wall and an API client that hits the same wall are told the same thing.

Status codes used deliberately:

* **409 Conflict** for `submissions_closed`. The request was well-formed and the caller was
  entitled to make it -- the *window* is what refused. 409 distinguishes that from 400
  (malformed) and 403 (not your resource).
* **404 Not Found** for anything the caller is not allowed to know exists, such as another
  team's draft. Returning 403 there would confirm the draft exists, which is itself a leak.
* **403 Forbidden** only when the caller's identity is known to the requester and the
  resource's existence is not a secret.
"""

from __future__ import annotations

from rest_framework import status as drf_status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class PortalError(Exception):
    """Base class for refusals that carry a stable error code.

    Views never build error bodies by hand; they raise one of these and let the exception
    handler (API) or the service caller (UI) turn it into a response. That is what keeps
    the API and the UI from drifting apart on what "closed" means.
    """

    code = "error"
    status_code = drf_status.HTTP_400_BAD_REQUEST
    message = "The request could not be completed."

    def __init__(self, message: str | None = None, **extra):
        self.message = message or self.message
        self.extra = extra
        super().__init__(self.message)

    def as_dict(self) -> dict:
        payload = {"error": self.code, "detail": self.message}
        payload.update({k: v for k, v in self.extra.items() if v is not None})
        return payload


class SubmissionsClosed(PortalError):
    """Raised by `core.deadlines` when a write arrives outside the submission window.

    `closed_at` is always included so a client can show the user the exact instant, and so
    an organizer reading the audit log can tell a late submission from an early one.
    """

    code = "submissions_closed"
    status_code = drf_status.HTTP_409_CONFLICT
    message = "Submissions are closed for this event."


class SubmissionsNotOpen(SubmissionsClosed):
    """The window has not started yet.

    Deliberately a subclass of SubmissionsClosed and deliberately reusing its `code`: the
    brief specifies one error code (`submissions_closed`) and one status (409) for both
    edges of the window, and an API client should not have to handle two spellings of "not
    now". The human-readable message still says which edge it was, and `opens_at` is
    attached instead of `closed_at`.
    """

    message = "Submissions have not opened for this event yet."


class PermissionDenied(PortalError):
    code = "permission_denied"
    status_code = drf_status.HTTP_403_FORBIDDEN
    message = "You do not have permission to do that."


class ValidationFailed(PortalError):
    code = "validation_failed"
    status_code = drf_status.HTTP_400_BAD_REQUEST
    message = "The submitted data was not valid."


class ConflictError(PortalError):
    """A legitimate request that collides with existing state.

    Used for team/invite refusals: already on a team, team full, invite spent.
    """

    code = "conflict"
    status_code = drf_status.HTTP_409_CONFLICT
    message = "That action conflicts with the current state."


def api_exception_handler(exc, context):
    """DRF exception handler that renders PortalError instances as their own code.

    Registered as `EXCEPTION_HANDLER` in settings. Anything that is not a PortalError falls
    through to DRF's default handling, so authentication and throttling responses keep their
    usual shape.
    """
    if isinstance(exc, PortalError):
        return Response(exc.as_dict(), status=exc.status_code)
    return drf_exception_handler(exc, context)
