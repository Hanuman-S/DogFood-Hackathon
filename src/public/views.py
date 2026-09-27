"""Pages anyone can see without logging in: home, events, and the project gallery.

All `never_cache`: counts and submissions change while the page is open, and a page brought back
with the Back button must be fetched again, not shown as it was."""

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache

from accounts.roles import is_judge_of
from events.models import Event
from events.services import can_manage, get_visible_event
from projects import gallery as gallery_query
from projects.models import Project
from projects.services import can_view


@never_cache
def home(request):
    return render(request, "public/home.html", {"project_count": gallery_query.visible_projects().count()})


@never_cache
def event_list(request):
    events = Event.objects.published().order_by("-submissions_open_at")
    return render(request, "public/event_list.html", {"events": events})


@never_cache
def event_detail(request, slug):
    event = get_visible_event(request.user, slug)
    return render(
        request, "public/event_detail.html",
        {
            "event": event,
            "tracks": event.visible_tracks(),
            "prizes": event.prizes.select_related("track"),
            "can_manage": can_manage(request.user, event),
            "is_judge": is_judge_of(request.user, event),
            "submitted_count": event.projects.filter(status="submitted").count(),
            "team_count": event.teams.count(),
        },
    )


@never_cache
def gallery(request):
    """/projects -- the public gallery. No login; only submitted projects of published events."""
    filters, page, facets = gallery_query.gallery(request.GET)
    return render(request, "public/gallery.html", {"filters": filters, "page": page, "facets": facets})


@never_cache
def project_detail(request, project_id):
    project = get_object_or_404(Project.objects.select_related("team", "event", "track"), pk=project_id)
    if not can_view(request.user, project):
        raise Http404("No such project.")
    return render(
        request, "projects/project_detail.html",
        {"project": project, "members": project.team.members.select_related("user")},
    )
