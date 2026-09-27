from django.urls import reverse

from accounts.roles import HOME_PORTAL_URL, role_of


def identity(request):
    """`role` and `portal_url` for the navigation bar and status line on every page."""
    user = getattr(request, "user", None)
    role = role_of(user)
    return {
        "role": role,
        "portal_url": reverse(HOME_PORTAL_URL[user.role]) if role != "visitor" else "",
    }
