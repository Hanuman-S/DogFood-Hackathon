from django.urls import reverse

from accounts.roles import PORTAL_URL, portals_of, role_of


def identity(request):
    """For the navigation bar and status line on every page.

    `role` is a one-word label (the home portal, or "visitor"); `portals` lists every portal
    this account may enter, since roles are per event and one person can hold several.
    """
    user = getattr(request, "user", None)
    role = role_of(user)
    portals = [(name, reverse(PORTAL_URL[name])) for name in portals_of(user)]
    return {
        "role": role,
        "portals": portals,
        "portal_url": portals[0][1] if portals else "",
    }
