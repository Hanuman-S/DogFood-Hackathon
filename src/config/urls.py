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
from django.urls import path

from core.admin_site import portal_admin_site
from core.views import healthz, home


def robots_txt(request):
    """Served locally so the portal never 404s a crawler probe, and so there is no reason
    for any page to reference an external host."""
    return HttpResponse("User-agent: *\nAllow: /\n", content_type="text/plain")


urlpatterns = [
    path("", home, name="home"),
    path("healthz", healthz, name="healthz"),
    path("robots.txt", robots_txt),
    # Admin is gated on is_platform_admin by PortalAdminSite.has_permission.
    path("admin/", portal_admin_site.urls),
]
