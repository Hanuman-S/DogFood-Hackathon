"""Two small middlewares: Bearer-token authentication and session activity tracking."""

from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone

from accounts.models import ApiToken, UserSession
from core import audit
from core.models import AuditAction


class BearerTokenMiddleware:
    """`Authorization: Bearer <token>` authenticates a request without a session.

    Rules:
    * A Bearer request is authenticated by the token alone; any session cookie is ignored.
    * A *bad* token is a hard 401, never a silent fall-back to "anonymous". A script with a
      revoked token should fail loudly, not quietly see the public view.
    * A header that is not the Bearer scheme ("Bearerabc", "Basic ...", a tab instead of the
      space) is not a token at all: the request stays a session request, with full CSRF checks
      (tests/test_comments.py pins this with a live session cookie and no CSRF token).
    * CSRF is not enforced for Bearer requests. CSRF exists because browsers attach cookies
      automatically; they never attach an Authorization header on their own, so a Bearer
      request cannot be forged cross-site. Session (cookie) requests keep full CSRF checks.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.auth_method = "session"
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() == "bearer":
            token = ApiToken.objects.authenticate(raw.strip())
            if token is None:
                audit.record(AuditAction.TOKEN_REJECTED, request=request, subject=raw[:8])
                response = JsonResponse(
                    {"error": "invalid_token", "detail": "Unknown or revoked API token."},
                    status=401,
                )
                response["WWW-Authenticate"] = 'Bearer error="invalid_token"'
                return response
            request.user = token.user
            request.auth_method = "bearer"
            request.api_token = token
            request._dont_enforce_csrf_checks = True
            _touch(ApiToken.objects.filter(pk=token.pk), "last_used_at")
        return self.get_response(request)


class SessionActivityMiddleware:
    """Keeps `UserSession.last_seen_at` roughly current: at most one UPDATE per session per
    SESSION_ACTIVITY_RESOLUTION, however many requests arrive."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/embed/"):
            return response  # an embed never reads or touches the session
        user = getattr(request, "user", None)
        if (
            getattr(request, "auth_method", "") == "session"
            and user is not None
            and user.is_authenticated
            and request.session.session_key
        ):
            _touch(
                UserSession.objects.filter(session_key=request.session.session_key),
                "last_seen_at",
            )
        return response


def _touch(queryset, field):
    """One UPDATE that is a no-op unless the timestamp is missing or older than the window."""
    now = timezone.now()
    stale = now - settings.SESSION_ACTIVITY_RESOLUTION
    queryset.filter(
        Q(**{f"{field}__lt": stale}) | Q(**{f"{field}__isnull": True})
    ).update(**{field: now})
