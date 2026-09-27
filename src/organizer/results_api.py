"""JSON API for the results flow (organizers of the event and platform admins; sessions and Bearer
tokens alike). The same services as the results page, so the same rules and audit rows.

    POST /api/events/<slug>/results/compute     {"kind": "preview" | "final"}   201
    POST /api/events/<slug>/results/publish     {"snapshot": <id>}              200
    POST /api/events/<slug>/results/unpublish                                   200
    POST /api/events/<slug>/results/settings    {"visibility": ..., "winners_top_n": n}   200

Refusals are {"error": <code>, "detail": ...} with the service's status: 401 / 403 from the portal
gate, 404 for an event the caller does not organize, then e.g. 409 judging_open, 409 voting_open,
409 final_predates_vote_close, 409 already_published, 400 invalid_result_settings.
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from core import audit
from core.api import BadRequest, error, read_json
from events.services import get_managed_event
from scoring import services
from scoring.errors import ScoringError
from voting.errors import VotingError


def _refusal(exc):
    if isinstance(exc, PermissionDenied):
        return error(403, "forbidden", str(exc))
    return error(exc.status, exc.code, exc.detail)


def _body(request):
    try:
        return read_json(request), None
    except BadRequest as exc:
        return None, error(400, "bad_request", str(exc))


def _snapshot_json(snapshot):
    return {"id": snapshot.pk, "kind": snapshot.kind, "method": snapshot.method,
            "vote_tally": snapshot.vote_tally_id, "final_weights": snapshot.final_weights,
            "created_at": snapshot.created_at.isoformat().replace("+00:00", "Z")}


@transaction.non_atomic_requests
@require_POST
@portal_required("organizer")
def compute(request, slug):
    event = get_managed_event(request.user, slug)
    data, bad = _body(request)
    if bad:
        return bad
    kind = data.get("kind")
    if kind not in ("preview", "final"):
        return error(400, "bad_request", 'kind must be "preview" or "final".')
    try:
        snapshot = services.compute_snapshot(event, kind, actor=request.user, request=request)
    except (ScoringError, VotingError, PermissionDenied) as exc:
        return _refusal(exc)
    return JsonResponse(_snapshot_json(snapshot), status=201)


@require_POST
@portal_required("organizer")
def publish(request, slug):
    event = get_managed_event(request.user, slug)
    data, bad = _body(request)
    if bad:
        return bad
    try:
        snapshot_id = int(data.get("snapshot"))
    except (TypeError, ValueError):
        return error(400, "bad_request", "snapshot must be a snapshot id.")
    try:
        publication = services.publish_results(event, snapshot_id, actor=request.user,
                                               origin=audit.origin_of(request))
    except (ScoringError, VotingError, PermissionDenied) as exc:
        return _refusal(exc)
    return JsonResponse({"published": publication.snapshot_id, "publication": publication.pk})


@require_POST
@portal_required("organizer")
def unpublish(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        publication = services.unpublish_results(event, actor=request.user, origin=audit.origin_of(request))
    except (ScoringError, VotingError, PermissionDenied) as exc:
        return _refusal(exc)
    return JsonResponse({"unpublished": publication.snapshot_id})


@require_POST
@portal_required("organizer")
def settings(request, slug):
    event = get_managed_event(request.user, slug)
    data, bad = _body(request)
    if bad:
        return bad
    try:
        row = services.set_result_settings(event, actor=request.user, origin=audit.origin_of(request),
                                           visibility=data.get("visibility"), winners_top_n=data.get("winners_top_n"))
    except (ScoringError, PermissionDenied) as exc:
        return _refusal(exc)
    return JsonResponse({"visibility": row.visibility, "winners_top_n": row.winners_top_n})
