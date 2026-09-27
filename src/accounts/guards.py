"""Backend access checks. Every portal view is wrapped in one of these.

A link hidden in the navigation is a courtesy; these decorators are the rule. They answer:

* not logged in  -> HTML: redirect to /login?next=...   API: 401 JSON
* wrong role     -> HTML: 403 page                      API: 403 JSON   (and an audit row)
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

from accounts.roles import PORTAL_ACCESS, role_of
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


def roles_required(*roles, portal=""):
    allowed = frozenset(roles)

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return _unauthenticated(request)
            if request.user.role not in allowed:
                audit.record(
                    AuditAction.ACCESS_DENIED, request=request,
                    subject=portal or request.path, role=role_of(request.user),
                )
                return forbidden(
                    request,
                    reason=f"the {portal or 'requested'} area is closed to the "
                    f"{role_of(request.user)} role.",
                )
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def portal_required(portal):
    """Gate a view to the roles `PORTAL_ACCESS` lists for `portal`."""
    return roles_required(*PORTAL_ACCESS[portal], portal=portal)
