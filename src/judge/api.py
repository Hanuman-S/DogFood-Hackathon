"""JSON API for judges: /api/judge/scores (sessions and Bearer tokens alike).

GET   the caller's own reviews, in every event they judge. 401 if not logged in; 403 (audited) if
      the caller judges no event.
      `?judge=<x>` is an explicit "read this judge's reviews" request. <x> resolves to one account
      in exactly three ways -- an email address (case-insensitive), an account id (digits), or a
      fixture judge id recorded by the importer (FixtureRef kind "judge", e.g. "jdg_02" -> that
      judge's membership in the imported event) -- and nothing else resolves. It is answered when
        * <x> is the caller (their own reviews), or
        * the caller organizes an event that <x> judges (then <x>'s reviews in the events the
          caller organizes; a platform admin organizes every event).
      Everything else -- an unknown <x>, another judge, an organizer of a different event -- is
      403 and an audit row.
POST  save or submit a review: {"event": slug, "project_id": id, "scores": {key: value},
      "comment": "...", "submit": true|false}, or declare a conflict of interest:
      {"event": slug, "project_id": id, "decline": "reason"}.
      Order: 404 unknown event -> 409 outside the judging window -> 403 not a judge / not
      assigned -> 400 invalid values.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from accounts.models import User
from accounts.roles import Role, event_roles, is_organizer_of
from core import audit
from core.api import BadRequest, error, read_json
from core.judging import JudgingNotOpen, check_judging_window, refusal_response
from core.models import AuditAction
from events.models import Event, EventMembership
from imports.models import FixtureRef
from projects.models import Project
from scoring import services as scoring


def resolve_judge(target):
    """The account `target` names, or None. Only three forms resolve; there is no fuzzy match."""
    target = (target or "").strip()
    if not target:
        return None
    if "@" in target:
        return User.objects.filter(email__iexact=target).first()
    if target.isdigit():
        return User.objects.filter(pk=int(target)).first()
    ref = FixtureRef.objects.filter(kind=FixtureRef.Kind.JUDGE, external_id=target).first()
    if ref is not None:
        membership = EventMembership.objects.filter(pk=ref.object_id, role=Role.JUDGE).select_related("user").first()
        return membership.user if membership else None
    return None


def _organized_events_judged_by(caller, judge):
    """Events `judge` judges that `caller` organizes (all of them for a platform admin)."""
    judged = Event.objects.filter(memberships__user=judge, memberships__role=Role.JUDGE)
    return [e for e in judged.distinct() if is_organizer_of(caller, e)]


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
    if Role.JUDGE not in event_roles(user) and not (request.GET.get("judge") and request.method == "GET"):
        audit.record(AuditAction.ACCESS_DENIED, request=request, subject="api:judge_scores",
                     reason="not a judge of any event")
        return error(403, "forbidden", "Only judges can read or write reviews here.")

    target = request.GET.get("judge")
    if target is None:
        if request.method == "GET":
            return _own(user)
        return _write(request)
    if request.method != "GET":
        return error(400, "bad_request", "'judge' applies to reading reviews only.")

    named = resolve_judge(target)
    if named is not None and named.pk == user.pk:
        return _own(user)
    events = _organized_events_judged_by(user, named) if named is not None else []
    if not events:
        audit.record(AuditAction.ACCESS_DENIED, request=request, subject=f"peer_scores:{target}"[:254],
                     reason="read another judge's reviews", resolved=named.pk if named else None)
        return error(403, "forbidden", "A judge can only read their own reviews; an organizer, those of "
                                       "their own event's judges.")
    reviews = [_review_json(s) for s in scoring.reviews_of(named).filter(judge__event__in=events)]
    return JsonResponse({"judge": named.email, "read_as": "organizer", "count": len(reviews), "scores": reviews})


def _own(user):
    if Role.JUDGE not in event_roles(user):
        return error(403, "forbidden", "Only judges can read or write reviews here.")
    reviews = [_review_json(s) for s in scoring.reviews_of(user)]
    return JsonResponse({"judge": user.email, "count": len(reviews), "scores": reviews})


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
