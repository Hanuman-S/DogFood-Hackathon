"""JSON API views.

T1 has exactly one **write** endpoint -- `POST /api/events/<slug>/projects`, the route advertised
as `submit` in `.dogfood.toml` -- and one read endpoint, `GET /api/projects`, the JSON face of the
public gallery. Editing and submitting are UI-only in T1 -- see the README's "Not done
yet". There are no stub endpoints here for anything unimplemented: an unimplemented T2 route 404s,
honestly, rather than answering something that looks like success.

**The order of checks is the point of this module.** Every write does:

    authenticate -> resolve the event (404) -> deadline (409) -> permission (403) -> validation (400)

The deadline is decided before the caller's team is resolved and before the request body is
examined, so a late POST is refused *as a late POST* -- never masked as "you are not on a team" or
"unknown field". That is why the body is validated on the line *after*
`services.guard_project_create` rather than in the same expression: argument evaluation would have
run the serializer first, and a late request with a body the portal does not recognise would have
come back 400. `tests/test_api_submit.py` pins the ordering with exactly that request.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.serializers import ProjectWriteSerializer
from core import clock
from core import permissions as perms
from gallery.selectors import GalleryQuery, gallery_page
from projects import services


class ProjectCreateView(APIView):
    """Create a project for the authenticated caller's team in this event."""

    def post(self, request, slug: str):
        if not request.user.is_authenticated:
            raise NotAuthenticated("Send an API token as `Authorization: Bearer <token>`.")

        # 404 first: there is no submission window to judge without an event.
        event = get_object_or_404(perms.visible_events(request.user), slug=slug)

        # Deadline, then permission. Both refusals are recorded in the audit log by the guards.
        team, now = services.guard_project_create(
            actor=request.user, event=event, request=request
        )

        # Only now is the body looked at.
        serializer = ProjectWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fields = dict(serializer.validated_data)

        project = services.create_project(
            actor=request.user,
            event=event,
            team=team,
            request=request,
            now=now,
            tags=fields.pop("tags", None),
            **fields,
        )
        return Response(_represent(project, request), status=status.HTTP_201_CREATED)


def _represent(project, request) -> dict:
    """The response body for a successful write.

    Small on purpose: the detail page is the full representation, and duplicating it here would be
    a second thing to keep in step. Timestamps go through `core.clock.iso`, so they end in `Z`
    rather than `+00:00` -- everything this portal emits is UTC and says so.
    """
    return {
        "id": project.pk,
        "name": project.name,
        "tagline": project.tagline,
        "status": project.status,
        "event": project.event.slug,
        "team": project.team.name,
        "submitted_at": clock.iso(project.submitted_at) if project.submitted_at else None,
        "missing_to_submit": services.missing_to_submit(project),
        "url": request.build_absolute_uri(f"/projects/{project.pk}"),
    }


class ProjectListView(APIView):
    """`GET /api/projects` -- the JSON face of the public gallery.

    Calls `gallery.selectors.gallery_page`, the same function the HTML gallery calls, with the same
    query parsing. Not "the same rules reimplemented in JSON": the same code. A second filter
    implementation is how an API ends up listing a draft the gallery correctly hid.

    Public, like the gallery, and viewer-independent for the same reason: the rows do not depend on
    who is asking, whether or not they sent a token.
    """

    def get(self, request):
        query = GalleryQuery.from_params(request.GET)
        result = gallery_page(query)
        return Response(
            {
                "results": [_project_summary(project, request) for project in result.projects],
                # Pagination metadata, so a client can page without guessing. `count` is the size
                # of the whole filtered set, not of this page.
                "count": result.total,
                "page": result.page.number,
                "pages": result.page.paginator.num_pages,
                "page_size": result.page.paginator.per_page,
                "next": _page_url(request, result.page.next_page_number())
                if result.page.has_next()
                else None,
                "previous": _page_url(request, result.page.previous_page_number())
                if result.page.has_previous()
                else None,
                # Echoed back so a client can see how its parameters were understood -- a
                # `sort=purple` that silently became `newest` is otherwise invisible.
                "query": {
                    "q": query.q,
                    "event": query.event,
                    "track": query.track,
                    "tag": query.tag,
                    "sort": query.sort,
                },
            }
        )


def _project_summary(project, request) -> dict:
    """One project as the gallery shows it.

    Tags come from the prefetched `project_tags`, so rendering a page of 50 adds no queries; going
    through `project.tags` here would be an N+1 that only shows up under load.
    """
    return {
        "id": project.pk,
        "name": project.name,
        "tagline": project.tagline,
        "event": project.event.slug,
        "team": project.team.name,
        "track": project.track.name if project.track_id else None,
        "tags": [project_tag.tag.name for project_tag in project.project_tags.all()],
        "submitted_at": clock.iso(project.submitted_at) if project.submitted_at else None,
        "thumbnail": request.build_absolute_uri(f"/projects/{project.pk}/thumbnail.img")
        if project.thumbnail
        else None,
        "url": request.build_absolute_uri(f"/projects/{project.pk}"),
    }


def _page_url(request, page_number: int) -> str:
    """This request's URL with `page` replaced, so every other filter is preserved."""
    params = request.GET.copy()
    params["page"] = page_number
    return request.build_absolute_uri(f"{request.path}?{params.urlencode()}")
