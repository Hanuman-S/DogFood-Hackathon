"""The public gallery's query: what is visible, how it is searched, filtered and sorted.

Shared by the gallery page (`/projects`) and the JSON API (`/api/projects`), so both always
agree on what a visitor may see and what a search returns.

Visibility: only *submitted* projects in *published* events. Drafts never appear here, however
the URL is crafted.

Search, on Postgres:
* full-text over name (weight A), tagline and team name (B) and description (C), English
  stemming, with web-search syntax: `"exact phrase"`, `-exclude`, `or`;
* plus plain substring matches on name, tagline, team and tags, so a partial word ("comp")
  still finds "Compass" -- full-text search alone matches whole words only.
Results with a query are ordered by relevance, then name.
"""

import re

from django.contrib.postgres.aggregates import StringAgg
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, OuterRef, Q, Subquery, TextField, Value
from django.db.models.functions import Coalesce

from events.models import Event, Track
from projects.models import Project, Status, Tag

PAGE_SIZE = 48
SORTS = {
    "newest": ("-submitted_at", "name"),
    "oldest": ("submitted_at", "name"),
    "name": ("name",),
}


def visible_projects():
    return (
        Project.objects.filter(status=Status.SUBMITTED, event__is_published=True)
        .select_related("team", "event", "track")
        .prefetch_related("tags")
    )


def _plain(q):
    """True for a simple search (no quotes, no -exclusions, no 'or'). Only those also get the
    substring fallback; otherwise "python -maps" would sneak "maps" projects back in."""
    return '"' not in q and not re.search(r"(^|\s)-\S", q) and " or " not in f" {q.lower()} "


def search(queryset, q):
    q = (q or "").strip()[:200]
    if not q:
        return queryset, False
    substring = (
        Q(name__icontains=q) | Q(tagline__icontains=q) | Q(team__name__icontains=q)
        | Q(pk__in=Project.objects.filter(tags__name__icontains=q).values("pk"))
    )
    if connection.vendor != "postgresql":
        return queryset.filter(substring), True
    tag_text = Coalesce(
        Subquery(
            Tag.objects.filter(projects=OuterRef("pk")).values("projects")
            .annotate(text=StringAgg("name", " ")).values("text")[:1],
            output_field=TextField(),
        ),
        Value("", output_field=TextField()),
        output_field=TextField(),
    )
    vector = (
        SearchVector("name", weight="A", config="english")
        + SearchVector("tagline", weight="B", config="english")
        + SearchVector("team__name", weight="B", config="english")
        + SearchVector(tag_text, weight="B", config="english")
        + SearchVector("description", weight="C", config="english")
    )
    query = SearchQuery(q, search_type="websearch", config="english")
    # Match with @@ (the `document=query` filter), rank only for ordering: ts_rank alone can
    # score a non-matching row above zero.
    matched = Q(document=query)
    if _plain(q):
        matched |= substring
    ranked = queryset.annotate(document=vector, rank=SearchRank(vector, query)).filter(matched)
    return ranked.order_by("-rank", "name"), True


class Filters:
    """The gallery's GET parameters, validated. Unknown values are ignored, never an error:
    a stale bookmark should show a gallery, not a 400."""

    def __init__(self, params):
        self.q = (params.get("q") or "").strip()[:200]
        self.event = Event.objects.published().filter(slug=params.get("event") or "").first()
        self.track = None
        track_id = params.get("track") or ""
        if self.event and track_id.isdigit():
            self.track = Track.objects.filter(event=self.event, pk=int(track_id), is_hidden=False).first()
        self.tag = (params.get("tag") or "").strip().lower()[:40]
        self.sort = params.get("sort") if params.get("sort") in SORTS else "newest"
        page = params.get("page") or "1"
        self.page = int(page) if page.isdigit() else 1

    def apply(self, queryset):
        if self.event:
            queryset = queryset.filter(event=self.event)
        if self.track:
            queryset = queryset.filter(track=self.track)
        if self.tag:
            queryset = queryset.filter(tags__name=self.tag)
        queryset, searched = search(queryset, self.q)
        if not searched:
            queryset = queryset.order_by(*SORTS[self.sort])
        return queryset

    def as_query(self, **overrides):
        """The current filters as a query string, with some values replaced (for links)."""
        from urllib.parse import urlencode

        values = {
            "q": self.q, "event": self.event.slug if self.event else "",
            "track": self.track.pk if self.track else "", "tag": self.tag,
            "sort": self.sort if self.sort != "newest" else "",
        }
        values.update(overrides)
        return urlencode({k: v for k, v in values.items() if v not in ("", None)})

    @property
    def active(self):
        return bool(self.q or self.event or self.track or self.tag)


def gallery(params):
    filters = Filters(params)
    results = filters.apply(visible_projects())
    paginator = Paginator(results, PAGE_SIZE)
    page = paginator.get_page(filters.page)

    scope = visible_projects().filter(event=filters.event) if filters.event else visible_projects()
    facets = {
        "events": Event.objects.published()
        .annotate(n=Count("projects", filter=Q(projects__status=Status.SUBMITTED)))
        .filter(n__gt=0).order_by("-submissions_close_at"),
        "tracks": (
            Track.objects.filter(event=filters.event, is_hidden=False)
            .annotate(n=Count("projects", filter=Q(projects__status=Status.SUBMITTED)))
            .order_by("order", "name")
            if filters.event else []
        ),
        "tags": Tag.objects.filter(projects__in=scope.values("pk"))
        .annotate(n=Count("projects")).order_by("-n", "name")[:20],
    }
    return filters, page, facets


def possible_duplicates(event):
    """Submissions in `event` that look like the same project twice: same name (ignoring case)
    or same repository URL. Shown to organizers, never acted on automatically -- two teams can
    legitimately fork one starter repo."""
    from django.db.models.functions import Lower

    groups = []
    for field, label in (("name", "name"), ("repo_url", "repository")):
        keyed = Project.objects.filter(event=event).exclude(**{field: ""}).annotate(key=Lower(field))
        keys = keyed.values("key").annotate(n=Count("id")).filter(n__gt=1).values_list("key", flat=True)
        for key in keys:
            projects = list(keyed.filter(key=key).select_related("team").order_by("submitted_at", "pk"))
            groups.append({"field": label, "value": projects[0].__dict__[field], "projects": projects})
    return groups
