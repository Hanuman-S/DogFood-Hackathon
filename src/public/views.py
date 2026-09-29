"""Pages anyone can see without logging in: home, events, and the project gallery.

All `never_cache`: counts and submissions change while the page is open, and a page brought back
with the Back button must be fetched again, not shown as it was."""

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache

from accounts.roles import is_judge_of
from events.models import Event
from events.services import can_manage, get_visible_event
from projects import comments
from projects import gallery as gallery_query
from projects.comment_errors import NotCommentable
from projects.models import Project
from projects.services import can_view
from scoring.results import results_page, results_visible
from teams.models import Team
from voting import services as voting_services
from voting.models import AccessMode
from core import deadlines
from core.deadlines import db_now


@never_cache
def home(request):
    return render(request, "public/home.html", {"project_count": gallery_query.visible_projects().count()})


@never_cache
def event_list(request):
    events = Event.objects.published().order_by("-submissions_open_at")
    return render(request, "public/event_list.html", {"events": events})


def _can_still_take_part(event, user):
    """Whether "enter as participant" / "sign up to take part" still leads anywhere: submissions
    are not closed for this viewer (their own team's extension counts)."""
    team = None
    if user.is_authenticated:
        team = Team.objects.filter(event=event, members__user=user).first()
    return not deadlines.window(event, team).is_closed


def _vote_button(event, user, now):
    """What the page offers for the community vote: "vote", "login", or None.

    Only while voting is open and only in the logged-in-accounts mode (the other modes vote
    through the link the organizers sent). An account is offered the button only if the vote
    would take it: the same rule the vote itself applies (voting.services.ineligibility: no
    staff or admins; with the organizers' option, only accounts created before voting opened)."""
    config = voting_services.voting_for(event)
    if not voting_services.is_open(config, now) or config.access_mode != AccessMode.AUTHENTICATED:
        return None
    if not user.is_authenticated:
        return "login"
    return "vote" if voting_services.ineligibility(event, config, user) is None else None


@never_cache
def event_detail(request, slug):
    event = get_visible_event(request.user, slug)
    now = db_now()
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
            "results_visible": results_visible(event, request.user),
            "can_take_part": _can_still_take_part(event, request.user),
            "vote_button": _vote_button(event, request.user, now),
        },
    )


@never_cache
def event_results(request, slug):
    """/events/<slug>/results. A 404 -- the same as for an event that does not exist -- unless the
    result is published with a public visibility, or the caller organizes the event (or is a
    platform admin), in which case they see a full preview. Rules: scoring.results."""
    event = get_visible_event(request.user, slug)
    page = results_page(event, request.user)
    if page is None:
        raise Http404("No such results.")
    return render(request, "public/results.html", {"event": event, "page": page})


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
    context = {"project": project, "members": project.team.members.select_related("user")}
    try:
        _, page, moderator = comments.comments_for(project.pk, request.user, request.GET.get("cpage"))
    except NotCommentable:
        pass  # a draft seen by its team: not in the gallery, so no comments
    else:
        context.update(comments_page=page, comments_moderator=moderator,
                       comments_open=project.event.comments_enabled and project.is_submitted
                       and project.event.is_published)
    return render(request, "projects/project_detail.html", context)
