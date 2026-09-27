"""JSON API for judges: /api/judge/scores (sessions and Bearer tokens alike).

GET   the caller's own reviews, in every event they judge.
      401 if not logged in; 403 (audited) if the caller judges no event.
      `?judge=<x>` is an explicit "read as another judge" request: it is answered only when <x>
      is the caller themself (their email or account id); anything else is refused with 403 and
      an audit row. There is no other way to read someone else's reviews here.
POST  save or submit a review: {"event": slug, "project_id": id, "scores": {key: value},
      "comment": "...", "submit": true|false}, or declare a conflict of interest:
      {"event": slug, "project_id": id, "decline": "reason"}.
      Order: 404 unknown event -> 409 outside the judging window -> 403 not a judge / not
      assigned -> 400 invalid values.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from accounts.roles import Role, event_roles
from core import audit
from core.api import BadRequest, error, read_json
from core.judging import JudgingNotOpen, check_judging_window, refusal_response
from core.models import AuditAction
from events.models import Event
from projects.models import Project
from scoring import services as scoring


def _names_caller(user, target):
    """Whether the `judge` parameter names the caller: their email or their account id, exactly."""
    target = (target or "").strip()
    return target.lower() == user.email.lower() or (target.isdigit() and int(target) == user.pk)


def _review_json(score):
    return {
        "id": score.pk,
        "event": score.judge.event.slug,
        "project_id": score.project_id,
        "project_name": score.project.name,
        "comment": score.comment,
        "criteria": {item.criterion.key: str(item.value) for item in score.items.all()},
        "submitted": score.submitted_at is not None,
        "submitted_at": score.submitted_at.isoformat().replace("+00:00", "Z") if score.submitted_at else None,
        "updated_at": score.updated_at.isoformat().replace("+00:00", "Z"),
    }


@require_http_methods(["GET", "POST"])
def judge_scores(request):
    user = request.user
    if not user.is_authenticated:
        response = error(401, "unauthenticated", "Log in or send a Bearer token.")
        response["WWW-Authenticate"] = "Bearer"
        return response
    if Role.JUDGE not in event_roles(user):
        audit.record(AuditAction.ACCESS_DENIED, request=request, subject="api:judge_scores",
                     reason="not a judge of any event")
        return error(403, "forbidden", "Only judges can read or write reviews here.")

    target = request.GET.get("judge")
    if target is not None and not _names_caller(user, target):
        audit.record(AuditAction.ACCESS_DENIED, request=request, subject=f"peer_scores:{target}"[:254],
                     reason="read another judge's reviews")
        return error(403, "forbidden", "A judge can only read their own reviews.")

    if request.method == "GET":
        reviews = [_review_json(s) for s in scoring.reviews_of(user)]
        return JsonResponse({"judge": user.email, "count": len(reviews), "scores": reviews})
    return _write(request)


def _write(request):
    try:
        data = read_json(request)
    except BadRequest as exc:
        return error(400, "bad_request", str(exc))
    slug, project_id = data.get("event"), data.get("project_id")
    if not slug or project_id in (None, ""):
        return error(400, "bad_request", "Send 'event' (the slug) and 'project_id'.")
    event = Event.objects.filter(slug=str(slug)).first()
    if event is None:
        return error(404, "not_found", "No such event.")
    decline = data.get("decline")
    submit = data.get("submit") is True
    try:
        # The window first: a late write is refused as late, whoever sends it.
        check_judging_window(request, event, action="decline" if decline else ("submit review" if submit else "save review"))
    except JudgingNotOpen as exc:
        return refusal_response(exc)

    membership = scoring.judge_membership(request.user, event)
    project = Project.objects.filter(pk=project_id, event=event).first() if str(project_id).isdigit() else None
    if membership is None or project is None:
        return error(403, "not_assigned", "This project is not in your queue.")

    try:
        if decline:
            assignment = scoring.live_assignment(membership, project)
            if assignment is None:
                return error(403, "not_assigned", "This project is not in your queue.")
            scoring.decline_assignment(request, assignment, str(decline))
            return JsonResponse({"ok": True, "declined": True, "project_id": project.pk})
        scores = data.get("scores") or {}
        if not isinstance(scores, dict):
            return error(400, "invalid_review", "'scores' must be an object of criterion key -> value.")
        score = scoring.save_review(request, membership, project, scores, str(data.get("comment") or ""),
                                    submit=submit)
    except JudgingNotOpen as exc:
        return refusal_response(exc)
    except scoring.ReviewError as exc:
        return error(exc.status, exc.code, str(exc))
    except scoring.AssignmentError as exc:
        return error(400, "invalid_decline", str(exc))
    return JsonResponse({"ok": True, "score_id": score.pk, "project_id": project.pk,
                         "submitted": score.submitted_at is not None})
