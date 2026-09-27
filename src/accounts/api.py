"""JSON endpoints for the accounts module."""

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

from accounts.guards import login_required
from accounts.roles import HOME_PORTAL_URL


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
            "role": user.role,
            "portal": reverse(HOME_PORTAL_URL[user.role]),
            "auth": request.auth_method,
        }
    )
