"""Root URL configuration.

Two routes here are load-bearing for the acceptance checker and are written **without a
trailing slash on purpose**: `/healthz` and `/projects`.

The checker calls them with `urllib`, which follows redirects -- and on a 301 or 302 it
rewrites a POST into a GET. If Django's `APPEND_SLASH` were answering for either route, the
checker would be measuring a redirect rather than the portal. So every path advertised in
`.dogfood.toml` is registered at exactly the string advertised, and `tests/test_acceptance_
contract.py` asserts each one answers in a single hop.
"""

from django.http import HttpResponse
from django.urls import include, path

from core.admin_site import portal_admin_site
from core.views import healthz, home
from teams import urls as teams_urls


def robots_txt(request):
    """Served locally so the portal never 404s a crawler probe, and so there is no reason
    for any page to reference an external host."""
    return HttpResponse("User-agent: *\nAllow: /\n", content_type="text/plain")


urlpatterns = [
    path("", home, name="home"),
    path("healthz", healthz, name="healthz"),
    path("robots.txt", robots_txt),
    # Auth and profile at the root: /login, /signup, /profile.
    path("", include("accounts.urls")),
    # Invite links live at the root so they stay short enough to paste into chat.
    #
    # Included as a bare pattern list rather than `include((patterns, "namespace"))`: the namespaced
    # form would make the URL name `invites:invite_accept`, and every `reverse("invite_accept")` in
    # the views would fail at runtime rather than at import time. No namespace means no ambiguity.
    path("", include(teams_urls.invite_urlpatterns)),
    path("events/", include("events.urls")),
    # `/events/<slug>/teams/new` belongs to the teams app but reads naturally under the event.
    path("events/", include(teams_urls.event_team_urlpatterns)),
    path("teams/", include("teams.urls")),
    # Admin is gated on is_platform_admin by PortalAdminSite.has_permission.
    path("admin/", portal_admin_site.urls),
]
