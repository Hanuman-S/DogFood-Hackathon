"""Backend access checks. Every portal view is wrapped in one of these.

A link hidden in the navigation is a courtesy; these decorators are the rule. They answer:

* not logged in  -> HTML: redirect to /login?next=...   API: 401 JSON
* wrong role     -> HTML: 403 page                      API: 403 JSON   (and an audit row)
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

from accounts.roles import can_enter, role_of
from core import audit
from core.models import AuditAction
from core.net import wants_json
from core.views import forbidden


def _unauthenticated(request):
    if wants_json(request):
        response = JsonResponse(
            {"error": "unauthenticated", "detail": "Log in or send a Bearer token."},
            status=401,
        )
        response["WWW-Authenticate"] = "Bearer"
        return response
    return redirect_to_login(request.get_full_path())


def login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _unauthenticated(request)
        return view(request, *args, **kwargs)

    return wrapped


def refuse(request, subject, reason):
    """A 403 with an audit row: the one way any portal says "not you"."""
    audit.record(
        AuditAction.ACCESS_DENIED, request=request, subject=subject, role=role_of(request.user),
    )
    return forbidden(request, reason=reason)


def portal_required(portal):
    """Gate a view to the accounts `PORTAL_ACCESS` lets into `portal`.

    This is the outer gate only. Views that act on one event also check the caller's role in
    *that* event (`roles.is_organizer_of`, `roles.can_compete_in`, ...).
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return _unauthenticated(request)
            if not can_enter(request.user, portal):
                return refuse(
                    request, portal,
                    f"the {portal} area is closed to this account. "
                    f"{PORTAL_REFUSAL.get(portal, '')}".strip(),
                )
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


PORTAL_REFUSAL = {
    "judge": "you are not a judge in any event.",
    "organizer": "you do not organize any event.",
    "admin": "only platform admins may enter.",
    "participant": "platform admins run every event, so they cannot compete.",
}
