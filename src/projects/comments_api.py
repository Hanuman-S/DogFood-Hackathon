"""JSON API for comments. The rules are projects/comments.py's; every refusal answers with the service
error's status and code (core.api.error), as the pages do.

    GET  /api/projects/<id>/comments?page=  list, newest first (anyone; the read rule decides what)
    POST /api/projects/<id>/comments         post {"body": "..."} -> 201 (logged in; else 401)
    POST /api/comments/<id>/delete           delete your own (soft)
    POST /api/comments/<id>/hide             {"reason": "..."} (organizers of the event, admins)
    POST /api/comments/<id>/restore          {"reason": "..."} (organizers of the event, admins)
"""

from functools import wraps

from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from accounts.guards import login_required, portal_required
from core import audit
from core.api import BadRequest, error, read_json

from . import comments
from .comment_errors import CommentError


def session_csrf(view):
    """CSRF for the callers it protects, and only them. CSRF exists because a browser attaches the
    session cookie to a forged request; a request with no session has nothing to forge, and a Bearer
    request is exempt already (accounts/middleware.py). So a logged-in session gets the full check,
    and an anonymous POST reaches the service -- which audits it and answers 401 -- instead of the
    middleware's 403."""

    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.user.is_authenticated and getattr(request, "auth_method", "session") == "session":
            refused = CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
            if refused is not None:
                return refused
        return view(request, *args, **kwargs)

    return wrapped


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def comment_json(comment, moderator=False):
    data = {
        "id": comment.pk,
        "project": comment.project_id,
        "author": comment.author.name,  # a display name only, never an email
        "body": comment.body,
        "created_at": _iso(comment.created_at),
    }
    if moderator:
        data.update(
            hidden=comment.hidden_at is not None, hidden_at=_iso(comment.hidden_at),
            hidden_by=comment.hidden_by.name if comment.hidden_by_id else None, hide_reason=comment.hide_reason,
            deleted=comment.deleted_at is not None, deleted_at=_iso(comment.deleted_at),
        )
    return data


def _refused(exc, request):
    response = error(exc.status, exc.code, exc.detail)
    if exc.status == 401:
        response["WWW-Authenticate"] = "Bearer"
    return response


@session_csrf
@never_cache
@require_http_methods(["GET", "POST"])
def project_comments(request, project_id):
    if request.method == "POST":
        try:
            data = read_json(request)
        except BadRequest as exc:
            return error(400, "bad_request", str(exc))
        try:
            comment = comments.post_comment(project_id, data.get("body"), author=request.user,
                                            origin=audit.origin_of(request))
        except CommentError as exc:
            return _refused(exc, request)
        return JsonResponse(comment_json(comment), status=201)
    try:
        project, page, moderator = comments.comments_for(project_id, request.user, request.GET.get("page"))
    except CommentError as exc:
        return _refused(exc, request)
    return JsonResponse({
        "project": project.pk,
        "comments_enabled": project.event.comments_enabled,
        "count": page.paginator.count, "page": page.number, "pages": page.paginator.num_pages,
        "results": [comment_json(c, moderator) for c in page],
    })


@session_csrf
@require_POST
@login_required
def comment_delete(request, comment_id):
    try:
        comment = comments.delete_own_comment(comment_id, actor=request.user, origin=audit.origin_of(request))
    except CommentError as exc:
        return _refused(exc, request)
    return JsonResponse(comment_json(comment, moderator=False) | {"deleted": True})


def _moderate(request, comment_id, action):
    try:
        data = read_json(request)
    except BadRequest as exc:
        return error(400, "bad_request", str(exc))
    try:
        comment = action(comment_id, data.get("reason"), actor=request.user, origin=audit.origin_of(request))
    except CommentError as exc:
        return _refused(exc, request)
    return JsonResponse(comment_json(comment, moderator=True))


@session_csrf
@require_POST
@portal_required("organizer")
def comment_hide(request, comment_id):
    return _moderate(request, comment_id, comments.hide_comment)


@session_csrf
@require_POST
@portal_required("organizer")
def comment_restore(request, comment_id):
    return _moderate(request, comment_id, comments.restore_comment)
