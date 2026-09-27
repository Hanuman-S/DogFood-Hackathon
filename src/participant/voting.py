"""The voter's page: /participant/events/<slug>/vote.

Gated by `portal_required("participant")` (any account but a platform admin), then by the voting
rules for *this* event (voting.services: the window, the event's judges and organizers, accounts
too new, your own team's project). The page never writes on GET: a ballot is created by the
"open my ballot" POST (or the first vote), so its project order is stored before it is shown.

The page never shows how anyone else voted, nor any tally.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from core import audit
from core.deadlines import db_now
from core.net import ip_hash
from events.services import get_visible_event
from voting import services
from voting.errors import VotingError
from voting.models import Method


def _voting_event(request, slug):
    event = get_visible_event(request.user, slug)
    config = services.voting_for(event)
    if config is None:
        raise Http404("This event has no community vote.")
    return event, config


@never_cache
@portal_required("participant")
def vote(request, slug):
    event, config = _voting_event(request, slug)
    voter = services.Voter(request.user)
    now = db_now()
    ballot, lines = services.ballot_view(event, voter)
    spent = sum(credits for _, _, credits in lines)
    return render(request, "participant/vote.html", {
        "event": event,
        "config": config,
        "state": services.state(config, now),
        "refusal": services.ineligibility(event, config, request.user),
        "ballot": ballot,
        "lines": lines,
        "spent": spent,
        "left": config.budget - spent,
        "quadratic": config.method == Method.QUADRATIC,
    })


@require_POST
@portal_required("participant")
def vote_open(request, slug):
    event, _ = _voting_event(request, slug)
    try:
        services.open_ballot(event, services.Voter(request.user), ip_hash=ip_hash(request),
                             origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    return redirect("participant:vote", slug=event.slug)


def lines_from_post(post, quadratic):
    """{project id: credits} from the ballot form: a number per project (quadratic), or the one
    project chosen (one person, one vote; "none" clears the vote)."""
    if quadratic:
        return {key[2:]: value.strip() for key, value in post.items() if key.startswith("p_")}
    choice = post.get("choice", "none")
    return {} if choice == "none" else {choice: 1}


@require_POST
@portal_required("participant")
def vote_cast(request, slug):
    event, config = _voting_event(request, slug)
    lines = lines_from_post(request.POST, config.method == Method.QUADRATIC)
    try:
        services.cast(event, services.Voter(request.user), ip_hash(request), lines, request.user,
                      origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "vote saved. you can change it until voting closes.")
    return redirect("participant:vote", slug=event.slug)
