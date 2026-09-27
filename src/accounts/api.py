"""JSON endpoints for the accounts module."""

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

from accounts.guards import login_required
from accounts.roles import PORTAL_URL, home_portal, portals_of


@require_GET
@login_required
def me(request):
    """Who am I? The quickest way to check that a session or Bearer token works:

        curl -H "Authorization: Bearer <token>" http://localhost:8080/api/me
    """
    user = request.user
    return JsonResponse(
        {
            "id": user.pk,
            "email": user.email,
            "name": user.name,
            "is_platform_admin": user.is_platform_admin,
            "can_create_events": user.can_create_events,
            # Roles are per event; this lists every one this account holds.
            "memberships": [
                {"event": m.event.slug, "role": m.role}
                for m in user.event_memberships.select_related("event").order_by("event__slug", "role")
            ],
            "portals": [reverse(PORTAL_URL[p]) for p in portals_of(user)],
            "portal": reverse(PORTAL_URL[home_portal(user)]),
            "auth": request.auth_method,
        }
    )
