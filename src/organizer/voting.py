"""Community voting for organizers: /organizer/events/<slug>/voting.

Set up the vote (window, method, budget, who may vote), end it early, remove it before it opens,
and read the live tally -- which only the event's organizers and platform admins can see, at any
time, on this page, as tally.csv and through /api/events/<slug>/votes/tally. Every view is gated
twice (`portal_required("organizer")`, then `get_managed_event`).
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.guards import portal_required
from core import audit
from core.csvfile import download, stamp, to_csv
from core.deadlines import db_now
from events.services import get_managed_event
from voting import services
from voting.errors import VotingError
from voting.forms import VotingConfigForm
from voting.models import Method

TALLY_HEADER = ["rank", "project", "team", "track", "influence", "ballots", "credits"]


def _initial(event, config):
    if config is None:
        return {"opens_at": services.effective_close(event), "closes_at": event.judging_ends_at,
                "method": Method.QUADRATIC, "credit_budget": 16, "access_mode": "authenticated",
                "accounts_before_open_only": True}
    return {"opens_at": config.opens_at, "closes_at": config.closes_at, "method": config.method,
            "credit_budget": config.credit_budget, "access_mode": config.access_mode,
            "accounts_before_open_only": config.accounts_before_open_only}


def _page(request, event, form=None, status=200):
    config = services.voting_for(event)
    now = db_now()
    return render(request, "organizer/voting.html", {
        "event": event,
        "config": config,
        "state": services.state(config, now),
        "form": form or VotingConfigForm(initial=_initial(event, config)),
        "tally": services.tally(event, request.user) if config else [],
        "counts": services.ballot_counts(event) if config else {},
        "effective_close": services.effective_close(event),
    }, status=status)


@never_cache
@portal_required("organizer")
def voting(request, slug):
    event = get_managed_event(request.user, slug)
    return _page(request, event)


@require_POST
@portal_required("organizer")
def voting_settings(request, slug):
    event = get_managed_event(request.user, slug)
    form = VotingConfigForm(request.POST)
    if not form.is_valid():
        return _page(request, event, form=form, status=400)
    data = form.cleaned_data
    try:
        services.set_voting_config(
            event, actor=request.user, origin=audit.origin_of(request),
            opens_at=data["opens_at"], closes_at=data["closes_at"], access_mode=data["access_mode"],
            method=data["method"], credit_budget=data["credit_budget"],
            accounts_before_open_only=data["accounts_before_open_only"],
        )
    except VotingError as error:
        form.add_error(None, str(error))
        return _page(request, event, form=form, status=error.status)
    messages.success(request, "voting saved.")
    return redirect("organizer:voting", slug=event.slug)


@require_POST
@portal_required("organizer")
def voting_end(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        services.end_voting_now(event, actor=request.user, origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "voting ended.")
    return redirect("organizer:voting", slug=event.slug)


@require_POST
@portal_required("organizer")
def voting_remove(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        services.remove_voting(event, actor=request.user, origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "voting removed.")
    return redirect("organizer:voting", slug=event.slug)


def _tally_rows(rows):
    return [[i, r.project.name, r.project.team.name, r.project.track.name if r.project.track else "",
             round(r.influence, 6), r.ballots, r.credits]
            for i, r in enumerate(rows, start=1)]


@never_cache
@require_GET
@portal_required("organizer")
def tally_csv(request, slug):
    event = get_managed_event(request.user, slug)
    if services.voting_for(event) is None:
        messages.error(request, "this event has no community vote.")
        return redirect("organizer:voting", slug=event.slug)
    rows = _tally_rows(services.tally_export(event, actor=request.user, origin=audit.origin_of(request)))
    return download(to_csv(TALLY_HEADER, rows), "text/csv; charset=utf-8", f"{event.slug}-tally-{stamp(db_now())}.csv")


@never_cache
@require_GET
@portal_required("organizer")
def tally_api(request, slug):
    """GET /api/events/<slug>/votes/tally -- organizers of the event and admins only (401 / 403 /
    404 otherwise, like every organizer view)."""
    event = get_managed_event(request.user, slug)
    config = services.voting_for(event)
    if config is None:
        return JsonResponse({"error": "no_voting", "detail": "This event has no community vote."}, status=404)
    try:
        rows = services.tally(event, request.user)
    except PermissionDenied:  # get_managed_event already refused; kept for the second gate
        return JsonResponse({"error": "forbidden"}, status=403)
    return JsonResponse({
        "event": event.slug, "state": services.state(config, db_now()), "method": config.method,
        "counts": services.ballot_counts(event),
        "projects": [{"project_id": r.project.pk, "project": r.project.name, "influence": round(r.influence, 6),
                      "ballots": r.ballots, "credits": r.credits} for r in rows],
    })
