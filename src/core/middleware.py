class SecurityHeadersMiddleware:
    """A strict Content-Security-Policy.

    Everything the UI needs is served from this origin, so the policy can forbid every other
    host. That doubles as a guarantee of the offline rule: a page that tried to load a CDN
    asset would be blocked in the browser, not just in review. No inline scripts or styles are
    used anywhere, so neither needs an exception.
    """

    POLICY = "; ".join(
        [
            "default-src 'self'",
            "img-src 'self' data:",
            "style-src 'self'",
            "script-src 'self'",
            "font-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
        ]
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", self.POLICY)
        return response


class DeadlineMiddleware:
    """Turns a refused late (or early) write into HTTP 409, wherever it was raised.

    Two sources: the service-layer check (`SubmissionsClosed` / `SubmissionsNotOpen`), and the
    database trigger, whose error arrives as a DatabaseError carrying a marker. Either way the
    caller gets the same answer: JSON for the API, a page for the browser, status 409.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.http import JsonResponse
        from django.shortcuts import render

        from core import audit, deadlines
        from core.models import AuditAction
        from core.net import wants_json

        if not isinstance(exception, (deadlines.SubmissionsClosed, deadlines.SubmissionsNotOpen)):
            caught = deadlines.trigger_refusal(exception)
            if caught is None:
                return None
            # The service check was bypassed or raced; the database said no. Record that too.
            audit.record(
                AuditAction.LATE_WRITE_REFUSED, request=request, subject=request.path,
                attempted="write refused by the database trigger",
                closed_at=caught.closed_at.isoformat() if caught.closed_at else "",
            )
            exception = caught

        if isinstance(exception, deadlines.SubmissionsNotOpen):
            body = {
                "error": "submissions_not_open",
                "detail": "Submissions for this event are not open yet.",
                "opens_at": exception.opens_at.isoformat().replace("+00:00", "Z"),
            }
        else:
            body = {
                "error": "submissions_closed",
                "detail": "Submissions for this event are closed. Nothing was saved.",
                "closed_at": exception.closed_at.isoformat().replace("+00:00", "Z") if exception.closed_at else None,
                "late_by_seconds": exception.late_by_seconds,
            }
        if wants_json(request):
            return JsonResponse(body, status=409)
        return render(
            request, "closed.html",
            {"error": exception, "body": body, "event": getattr(exception, "event", None)},
            status=409,
        )
