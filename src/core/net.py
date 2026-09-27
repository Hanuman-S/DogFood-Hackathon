"""Request helpers shared by every app."""

from django.conf import settings


def client_ip(request):
    """The caller's IP address.

    X-Forwarded-For is client-supplied, so it is trusted only when TRUST_PROXY_HEADERS says a
    proxy we run sets it. Trusting it blindly would let anyone choose the IP the login throttle
    counts against -- and lock a different user out.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def user_agent(request):
    return request.META.get("HTTP_USER_AGENT", "")[:300]


def wants_json(request):
    """True for API callers: anything under /api/, or authenticated by Bearer token."""
    return request.path.startswith("/api/") or getattr(request, "auth_method", "") == "bearer"
