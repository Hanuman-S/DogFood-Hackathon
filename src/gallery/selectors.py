"""The gallery's one query.

Both doors -- the server-rendered pages in `gallery/views.py` and `GET /api/projects` -- call
`gallery_page()`. Not "they apply the same rules": they run the same function. A second
implementation of the filtering is how a JSON endpoint ends up listing a draft that the HTML
gallery correctly hid.

**The scoper is viewer-independent, and that is load-bearing.** This module calls
`permissions.gallery_projects()` and never `visible_projects(user)`. It takes no user at all, so it
cannot accidentally become viewer-dependent. The reason is a participant trap rather than a
security one: if a team saw their own unsubmitted draft in the public listing, they would reasonably
conclude it was public and never press submit. An organizer seeing hidden projects in the gallery
would likewise have no way to tell what the public actually sees. Drafts are reachable at
`/projects/<id>` for the people entitled to them -- that page *is* viewer-dependent -- but the
listing shows one thing to everyone.

**Tolerant parsing.** Every parameter arrives from a URL somebody may have typed, truncated or
bookmarked from an older version of the portal. Nothing here may raise:

* A malformed *scalar* (`page=abc`, `sort=purple`) falls back to the default.
* A *filter* naming something that does not exist (`track=99999`, `tag=nope`, `event=gone`) simply
  matches nothing. It is deliberately **not** ignored: dropping an unresolvable filter would widen
  the result set and show projects the visitor did not ask for, which is worse than an honest empty
  page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.paginator import Page, Paginator
from django.db.models import QuerySet

from core import permissions as perms
from projects.search import search_projects
from projects.services import normalize_tag

# Control characters are stripped from every text parameter -- see `_clean_text`. Built from
# code points instead of a written escape sequence, because a literal control character in this
# file would make the module unimportable.
_CONTROL_CHARS = re.compile(
    "[" + re.escape("".join(chr(code) for code in list(range(32)) + [127])) + "]"
)

# Sort key -> the ordering it applies. **Every ordering ends in a primary-key tiebreak.**
#
# Without one, rows that tie on the leading column (two projects submitted in the same second, or
# two projects called "Untitled") come back in whatever order Postgres finds convenient, which can
# differ between two requests for the same page. A visitor paging through then sees one project
# twice and another not at all, because LIMIT/OFFSET slices an unstable ordering. `id` is unique
# and never changes, so it makes the order total.
SORTS = {
    "newest": ("-submitted_at", "-id"),
    "name": ("name", "id"),
}
DEFAULT_SORT = settings.GALLERY_DEFAULT_SORT if settings.GALLERY_DEFAULT_SORT in SORTS else "newest"

# The same keys with the labels the UI shows. A list of pairs rather than a dict comprehension over
# SORTS so the wording lives next to the ordering it describes.
SORTS_LABELS = [("newest", "Newest first"), ("name", "Name A–Z")]


@dataclass(frozen=True)
class GalleryQuery:
    """The parsed, validated query. Built only by `from_params`, so it is always coherent."""

    q: str = ""
    event: str = ""
    track: int | None = None
    tag: str = ""
    sort: str = DEFAULT_SORT
    page: int = 1

    @classmethod
    def from_params(cls, params, *, event: str = "") -> GalleryQuery:
        """Parse a `request.GET` (or any mapping) without ever raising.

        Args:
            event: an event slug from the URL path, for `/events/<slug>/projects`. It wins over a
                `?event=` parameter, because the path is what the visitor navigated to.
        """
        sort = _clean_text(params.get("sort")).lower()
        return cls(
            q=_clean_text(params.get("q"))[:200],
            event=_clean_text(event or params.get("event"))[:200],
            track=_safe_int(params.get("track")),
            # Normalized the same way tags are stored, so `?tag=React` finds `react`.
            tag=normalize_tag(_clean_text(params.get("tag")))[:60],
            sort=sort if sort in SORTS else DEFAULT_SORT,
            page=_safe_int(params.get("page")) or 1,
        )

    @property
    def is_filtered(self) -> bool:
        return bool(self.q or self.event or self.track or self.tag)

    def as_params(self, **overrides) -> dict[str, Any]:
        """The query as URL parameters, for building links that keep the current state.

        Every piece of state lives in the query string -- there is no session-held filter, no
        cookie and no POST -- so a filtered, sorted, paginated view is a URL somebody can paste
        into chat or bookmark.
        """
        params = {
            "q": self.q,
            "event": self.event,
            "track": self.track,
            "tag": self.tag,
            "sort": self.sort,
            "page": self.page,
        }
        params.update(overrides)
        return {key: value for key, value in params.items() if value not in (None, "", 1)}


@dataclass
class GalleryResult:
    """What both doors render."""

    query: GalleryQuery
    page: Page
    total: int
    facets: dict = field(default_factory=dict)

    @property
    def projects(self) -> list:
        return list(self.page.object_list)


def gallery_queryset(query: GalleryQuery) -> QuerySet:
    """The filtered, ordered queryset -- no pagination, no related fetching.

    Separate from `gallery_page` so a test (or a future export) can assert on the set of rows
    without paging, and so the ordering is visible in one place.
    """
    queryset = perms.gallery_projects()

    if query.event:
        queryset = queryset.filter(event__slug=query.event)
    if query.track is not None:
        queryset = queryset.filter(track_id=query.track)
    if query.tag:
        # Exact match on the normalized tag name, not a search-vector match. The vector stems and
        # tokenizes ("reacts" and "react" collapse), which is right for prose and wrong for a
        # filter: clicking the `react` pill must mean exactly that tag, or the count shown next to
        # it will not match the number of results.
        queryset = queryset.filter(project_tags__tag__name=query.tag)
    if query.q:
        queryset = search_projects(queryset, query.q)

    # `distinct()` because the tag join can multiply rows when a project carries several tags. It
    # comes before `order_by` so the ordering columns are part of the DISTINCT.
    return queryset.distinct().order_by(*SORTS[query.sort])


def gallery_page(query: GalleryQuery, *, with_facets: bool = False) -> GalleryResult:
    """Run the gallery query and return one page of it.

    Related rows are fetched up front: without `select_related`/`prefetch_related`, rendering 50
    cards issues 50 queries for the team name plus 50 more for the tags, and the page that a judge
    reloads most often is the slowest thing in the portal. `tests/test_gallery.py` pins the query
    count so a template change cannot quietly reintroduce that.
    """
    queryset = gallery_queryset(query).select_related("event", "team", "track").prefetch_related(
        "project_tags__tag"
    )

    paginator = Paginator(queryset, settings.GALLERY_PAGE_SIZE)
    # `get_page` is the tolerant form: a page number that is not a number, is zero, is negative or
    # runs past the end returns the first or last page instead of raising.
    page = paginator.get_page(query.page)

    return GalleryResult(
        query=query,
        page=page,
        total=paginator.count,
        facets=_facets(query) if with_facets else {},
    )


def _facets(query: GalleryQuery) -> dict:
    """The options the filter form offers.

    Drawn from the gallery-visible set rather than from every row in the database, so the form
    never offers a track or tag that would return nothing. Scoped to the chosen event when there is
    one, for the same reason.
    """
    from events.models import Event, Track
    from projects.models import Tag

    visible = perms.gallery_projects()
    if query.event:
        visible = visible.filter(event__slug=query.event)

    return {
        "events": Event.objects.filter(
            gallery_public=True, projects__in=visible
        ).distinct().order_by("name"),
        "tracks": Track.objects.filter(projects__in=visible).distinct().order_by("name"),
        "tags": Tag.objects.filter(project_tags__project__in=visible)
        .distinct()
        .order_by("name"),
        "sorts": SORTS_LABELS,
    }


def _clean_text(raw) -> str:
    """Strip whitespace and control characters from a text parameter.

    The control-character removal is not tidiness. Postgres rejects a **null byte** anywhere in a
    query parameter with `ValueError: A string literal cannot contain NUL characters`, raised by the
    driver before the query is even sent -- so `?q=glass%00signal`, which anyone can type, was a
    500 until this existed. The other C0 characters are stripped for the same reason in spirit: they
    cannot match anything in a project name, and they make log lines unreadable.
    """
    text = str(raw or "")
    return _CONTROL_CHARS.sub("", text).strip()


def _safe_int(raw) -> int | None:
    """An int, or None. Never an exception: this parses a URL a human may have edited."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # Negative and absurd values are treated as absent rather than clamped, so a hand-edited URL
    # behaves like no filter at all instead of silently meaning something else.
    if value < 1 or value > 2_147_483_647:
        return None
    return value
