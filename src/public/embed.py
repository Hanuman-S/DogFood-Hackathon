"""The embeddable gallery (C3): /embed/events/<slug>/gallery, for another site to show an event's
projects in an iframe.

What it shows is exactly what /projects shows an anonymous visitor for that event (the
viewer-independent projects.gallery.visible_projects()): only a published event, only submitted
projects, never a draft. It never reads who is asking -- it is rendered without the request, so no
context processor can touch the session or a CSRF token -- and it shows no vote counts, results,
ranks or comment counts.

Query parameters, each falling back to its default on a bad value (never an error):
    track   a visible track's id           (default: every track)
    tag     a tag name                     (default: every tag)
    sort    newest | oldest | name         (default: newest)
    limit   1..24                          (default: 12)
    theme   dark | light                   (default: dark)

This is the one route that may be framed: its own Content-Security-Policy says frame-ancestors *, and
it has no X-Frame-Options. Every other route keeps frame-ancestors 'none' and DENY. It sets no cookie
at all (core.middleware.EmbedMiddleware clears any on /embed/), so embedding it never creates a
third-party cookie. Project links open the portal in a new tab.
"""

from django.conf import settings
from django.http import Http404, HttpResponse, QueryDict
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

from core.middleware import SecurityHeadersMiddleware
from events.models import Event
from projects.gallery import Filters, visible_projects

LIMIT_DEFAULT, LIMIT_MAX = 12, 24
THEMES = ("dark", "light")
EMBED_POLICY = SecurityHeadersMiddleware.POLICY.replace("frame-ancestors 'none'", "frame-ancestors *")


def _limit(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return LIMIT_DEFAULT
    return min(max(n, 1), LIMIT_MAX)


@never_cache
@require_GET
@xframe_options_exempt
def gallery(request, slug):
    event = Event.objects.published().filter(slug=slug).first()
    if event is None:
        raise Http404("No such event.")
    params = QueryDict(mutable=True)
    params.update({"event": event.slug, "track": request.GET.get("track", ""), "tag": request.GET.get("tag", ""),
                   "sort": request.GET.get("sort", "")})
    filters = Filters(params)  # unknown values are ignored, as on /projects
    projects = list(filters.apply(visible_projects().filter(event=event))[:_limit(request.GET.get("limit"))])
    theme = request.GET.get("theme") if request.GET.get("theme") in THEMES else "dark"
    base = settings.PORTAL_BASE_URL.rstrip("/")
    html = render_to_string("public/embed_gallery.html", {
        "event": event, "projects": projects, "theme": theme, "base": base, "filters": filters,
    })  # no request: nothing here may depend on, or touch, who is asking
    response = HttpResponse(html)
    response["Content-Security-Policy"] = EMBED_POLICY
    return response
