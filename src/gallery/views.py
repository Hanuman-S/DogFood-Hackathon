"""The public gallery.

`/projects` is the route `.dogfood.toml` advertises as `gallery`, registered at exactly that string
at the root of the URLconf -- not under the `projects/` include, which owns `/projects/<id>`.

Both views are thin: parse the query string into a `GalleryQuery`, hand it to
`gallery.selectors.gallery_page`, render. No filtering logic lives here, because
`GET /api/projects` must return the same rows and does not go through this module.

**No authentication anywhere in this file.** The gallery is public, shows the same rows to everyone,
and takes no user argument -- see the note on viewer-independence in `selectors.py`.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404, render

from events.models import Event
from gallery.selectors import SORTS_LABELS, GalleryQuery, gallery_page


def project_gallery(request):
    """`GET /projects` -- every publicly visible project, across every public event."""
    query = GalleryQuery.from_params(request.GET)
    result = gallery_page(query, with_facets=True)
    return render(request, _template_for(request), _context(result))


def event_project_gallery(request, slug: str):
    """`GET /events/<slug>/projects` -- the same gallery, scoped to one event by its path.

    The event is resolved against the publicly visible set, so an event whose gallery is switched
    off is a 404 here rather than an empty page: the difference matters to an organizer checking
    whether their own switch took effect.
    """
    event = get_object_or_404(Event.objects.filter(gallery_public=True), slug=slug)
    query = GalleryQuery.from_params(request.GET, event=event.slug)
    result = gallery_page(query, with_facets=True)
    return render(request, _template_for(request), _context(result, event=event))


def _context(result, *, event: Event | None = None) -> dict:
    query = result.query
    return {
        "event": event,
        "query": query,
        "page": result.page,
        "projects": result.projects,
        "total": result.total,
        "facets": result.facets,
        "is_filtered": query.is_filtered,
        # Prebuilt so the template never assembles a query string by hand, which is how a filter
        # silently gets dropped from a pagination or sort link.
        "sort_options": [
            {
                "key": key,
                "label": label,
                "href": _querystring(query.as_params(sort=key, page=1)),
                "current": key == query.sort,
            }
            for key, label in SORTS_LABELS
        ],
        "next_link": _querystring(query.as_params(page=result.page.next_page_number()))
        if result.page.has_next()
        else "",
        "previous_link": _querystring(query.as_params(page=result.page.previous_page_number()))
        if result.page.has_previous()
        else "",
        "clear_link": "?" if query.is_filtered else "",
    }


def _querystring(params: dict) -> str:
    from urllib.parse import urlencode

    encoded = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"?{encoded}" if encoded else "?"


def _template_for(request) -> str:
    """Full page normally; just the results region for an HTMX request.

    This is the one place the portal looks at a request header to decide what to render, so it is
    worth being explicit about why it is not the forbidden kind of branch: `HX-Request` is set by
    the portal's own vendored htmx on a request the visitor's browser makes to this same view, and
    both branches render the same rows from the same query. Nothing about the response *content*
    depends on it -- only whether the surrounding page furniture is repeated. The filter form works
    as a plain GET form with no JavaScript at all; htmx only avoids a full page repaint.
    """
    if request.headers.get("HX-Request") == "true":
        return "gallery/_results.html"
    return "gallery/project_list.html"
