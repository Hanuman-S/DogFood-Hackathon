"""JSON API for events and projects. Same service functions as the pages, so the same rules.

    GET   /api/events                          published events
    GET   /api/events/<slug>                   one event, with tracks, prizes and questions
    POST  /api/events/<slug>/projects          start your team's project (participant)
    GET   /api/projects                        the public gallery: ?q= &event= &track= &tag= &sort= &page=
    GET   /api/projects/<id>                   read a project you may see (public once submitted)
    PATCH /api/projects/<id>                   edit (team members); POST works too
    POST  /api/projects/<id>/submit            draft -> submitted
    POST  /api/projects/<id>/unsubmit          submitted -> draft

Authenticate with `Authorization: Bearer <token>` (no CSRF needed) or a session cookie (CSRF
token required). Errors are JSON: {"error": <code>, "detail": <sentence>, ...}.
"""

from django.http import JsonResponse, QueryDict
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.guards import login_required
from core import deadlines
from core.api import BadRequest, error, form_errors, read_json
from events.models import Event
from events.services import can_manage, get_visible_event
from projects import services
from projects.forms import ProjectForm
from projects.models import Project
from teams.services import TeamRuleError


def _dt(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def event_json(event, detail=False):
    data = {
        "slug": event.slug,
        "name": event.name,
        "tagline": event.tagline,
        "phase": event.phase,
        "starts_at": _dt(event.starts_at),
        "submissions_open_at": _dt(event.submissions_open_at),
        "submissions_close_at": _dt(event.submissions_close_at),
        "judging_ends_at": _dt(event.judging_ends_at),
        "min_team_size": event.min_team_size,
        "max_team_size": event.max_team_size,
    }
    if detail:
        data["description"] = event.description
        data["tracks"] = [{"id": t.pk, "name": t.name, "description": t.description} for t in event.visible_tracks()]
        data["prizes"] = [
            {"title": p.title, "value": p.value, "rank": p.rank, "track": p.track_id}
            for p in event.prizes.all()
        ]
        data["questions"] = [
            {"id": q.pk, "prompt": q.prompt, "kind": q.kind, "required": q.required,
             "choices": q.choice_list()}
            for q in event.visible_questions()
        ]
    return data


def project_json(project):
    return {
        "id": project.pk,
        "event": project.event.slug,
        "team": {"id": project.team_id, "name": project.team.name},
        "name": project.name,
        "tagline": project.tagline,
        "description": project.description,
        "track": project.track_id,
        "repo_url": project.repo_url,
        "demo_video_url": project.demo_video_url,
        "live_url": project.live_url,
        "tags": list(project.tags.values_list("name", flat=True)),
        "answers": {str(a.question_id): a.value for a in project.answers.all()},
        "thumbnail": f"/media/{project.thumbnail.name}" if project.thumbnail else None,
        "images": [{"url": f"/media/{i.image.name}", "caption": i.caption} for i in project.images.all()],
        "status": project.status,
        "submitted_at": _dt(project.submitted_at),
        "updated_at": _dt(project.updated_at),
        "missing_for_submission": services.missing_for_submission(project),
    }


@require_GET
def events(request):
    return JsonResponse({"events": [event_json(e) for e in Event.objects.published()]})


@require_GET
def event_detail(request, slug):
    return JsonResponse(event_json(get_visible_event(request.user, slug), detail=True))


@require_POST
@login_required
def project_create(request, slug):
    event = get_visible_event(request.user, slug)
    try:
        data = read_json(request)
        project = services.start_project(request, event, data.get("name", ""))
    except BadRequest as exc:
        return error(400, "bad_request", str(exc))
    except (services.ProjectRuleError, TeamRuleError) as exc:
        return error(409, "refused", str(exc))
    if len(data) > 1:  # anything besides the name: apply it as a first edit
        response = _apply_edit(request, project, data)
        if response.status_code != 200:
            return response
    project.refresh_from_db()
    return JsonResponse(project_json(project), status=201)


def _get_project(request, project_id):
    project = Project.objects.select_related("team", "event").filter(pk=project_id).first()
    if project is None or not services.can_view(request.user, project):
        return None
    return project


def _apply_edit(request, project, data):
    """Merge `data` over the project's current values and run it through the same form the
    edit page uses, so partial updates (PATCH semantics) get identical validation."""
    form = ProjectForm(instance=project, event=project.event)
    current = {name: form[name].value() for name in form.fields if name not in ("thumbnail_upload", "remove_thumbnail")}
    current = {k: ("" if v is None else v) for k, v in current.items()}
    for key in ("name", "tagline", "description", "track", "repo_url", "demo_video_url", "live_url"):
        if key in data:
            current[key] = "" if data[key] is None else data[key]
    if "tags" in data:
        tags = data["tags"]
        current["tags"] = ", ".join(tags) if isinstance(tags, list) else str(tags)
    for qid, value in (data.get("answers") or {}).items():
        current[f"q_{qid}"] = value
    for key, value in list(current.items()):
        if isinstance(value, bool):  # checkbox answers: False must be *absent* in form data
            if value:
                current[key] = "on"
            else:
                current.pop(key)
    form = ProjectForm(_to_querydict(current), instance=project, event=project.event)
    if not form.is_valid():
        return form_errors(form)
    try:
        services.update_project(request, project, form)
    except services.ProjectRuleError as exc:
        return error(409, "incomplete", str(exc), missing=getattr(exc, "missing", {}))
    return JsonResponse({"ok": True})


def _to_querydict(data):
    query = QueryDict(mutable=True)
    for key, value in data.items():
        query[key] = str(value)
    return query


def gallery_json(project):
    """The public view of a project: no draft-only fields, no submission checklist."""
    return {
        "id": project.pk,
        "url": f"/projects/{project.pk}",
        "event": project.event.slug,
        "team": project.team.name,
        "name": project.name,
        "tagline": project.tagline,
        "track": project.track.name if project.track else None,
        "tags": [t.name for t in project.tags.all()],
        "repo_url": project.repo_url,
        "demo_video_url": project.demo_video_url,
        "live_url": project.live_url,
        "thumbnail": f"/media/{project.thumbnail.name}" if project.thumbnail else None,
        "submitted_at": _dt(project.submitted_at),
    }


@require_GET
def projects(request):
    """The gallery as JSON: same visibility, search and filters as /projects."""
    from projects import gallery

    filters, page, _ = gallery.gallery(request.GET)
    return JsonResponse({
        "count": page.paginator.count,
        "page": page.number,
        "pages": page.paginator.num_pages,
        "results": [gallery_json(p) for p in page.object_list],
    })


@require_http_methods(["GET", "PATCH", "POST"])
def project_detail(request, project_id):
    if request.method != "GET" and not request.user.is_authenticated:
        response = error(401, "unauthenticated", "Log in or send a Bearer token.")
        response["WWW-Authenticate"] = "Bearer"
        return response
    project = _get_project(request, project_id)
    if project is None:
        return error(404, "not_found", "No such project.")
    if request.method == "GET":
        # Team members and organizers get the full record (with the submission checklist);
        # everyone else gets the public view.
        if services.can_edit(request.user, project) or can_manage(request.user, project.event):
            return JsonResponse(project_json(project))
        return JsonResponse(gallery_json(project))
    deadlines.check_submission_window(request, project.event, project.team, action="edit the project", needs_open=True)
    if not services.can_edit(request.user, project):
        return error(403, "forbidden", "Only members of this team can edit its project.")
    try:
        data = read_json(request)
    except BadRequest as exc:
        return error(400, "bad_request", str(exc))
    response = _apply_edit(request, project, data)
    if response.status_code != 200:
        return response
    project.refresh_from_db()
    return JsonResponse(project_json(project))


def _transition(request, project_id, action):
    project = _get_project(request, project_id)
    if project is None:
        return error(404, "not_found", "No such project.")
    deadlines.check_submission_window(request, project.event, project.team, action=action.__name__, needs_open=True)
    if not services.can_edit(request.user, project):
        return error(403, "forbidden", "Only members of this team can do that.")
    try:
        action(request, project)
    except services.ProjectRuleError as exc:
        return error(409, "refused", str(exc), missing=getattr(exc, "missing", {}))
    project.refresh_from_db()
    return JsonResponse(project_json(project))


@require_POST
@login_required
def project_submit(request, project_id):
    return _transition(request, project_id, services.submit_project)


@require_POST
@login_required
def project_unsubmit(request, project_id):
    return _transition(request, project_id, services.unsubmit_project)
