"""JSON API for voters (sessions and Bearer tokens alike; the same services as the page).

    GET   /api/events/<slug>/ballot    the vote's window and rules, and the caller's own ballot
                                       (null until they open or cast one; a GET never creates it)
    POST  /api/events/<slug>/ballot    {"lines": {"<project id>": credits, ...}} sets the caller's
                                       whole ballot (projects not named get 0); {"lines": {}} just
                                       opens it. 200 with the ballot.

Refusals, in this order: 401 not logged in; 403 platform admin (portal); 404 no such event or no
vote; 409 voting_not_open / voting_closed; 403 staff_cannot_vote / account_too_new / own_project;
400 invalid_ballot / over_budget. Nothing here shows a tally or anyone else's ballot.
"""

from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts.guards import portal_required
from core import audit
from core.api import BadRequest, error, read_json
from core.deadlines import db_now
from core.net import ip_hash
from events.services import get_visible_event
from voting import services
from voting.errors import VotingError


def _dt(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def ballot_json(event, config, voter):
    ballot, lines = services.ballot_view(event, voter)
    return {
        "event": event.slug,
        "state": services.state(config, db_now()),
        "opens_at": _dt(config.opens_at),
        "closes_at": _dt(config.closes_at),
        "method": config.method,
        "budget": config.budget,
        "ballot": None if ballot is None else {
            "id": ballot.pk,
            "spent": sum(c for _, _, c in lines),
            "lines": [{"position": pos, "project_id": p.pk, "project": p.name, "credits": c} for pos, p, c in lines],
        },
    }


@never_cache
@require_http_methods(["GET", "POST"])
@portal_required("participant")
def ballot(request, slug):
    event = get_visible_event(request.user, slug)
    config = services.voting_for(event)
    if config is None:
        raise Http404("This event has no community vote.")
    voter = services.Voter(request.user)
    if request.method == "POST":
        try:
            data = read_json(request)
        except BadRequest as exc:
            return error(400, "bad_request", str(exc))
        lines = data.get("lines", {})
        try:
            if lines == {}:
                services.open_ballot(event, voter, ip_hash=ip_hash(request), origin=audit.origin_of(request))
            else:
                services.cast(event, voter, ip_hash(request), lines, request.user, origin=audit.origin_of(request))
        except VotingError as exc:
            return error(exc.status, exc.code, exc.detail)
    return JsonResponse(ballot_json(event, config, voter))
