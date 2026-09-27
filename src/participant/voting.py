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
from django.urls import reverse
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


def ballot_page(request, event, config, voter, *, via, open_url, cast_url):
    """The ballot page for any access mode (templates/vote.html). Reads only."""
    ballot, lines = services.ballot_view(event, voter)
    spent = sum(credits for _, _, credits in lines)
    return render(request, "vote.html", {
        "event": event,
        "config": config,
        "state": services.state(config, db_now()),
        "wrong_mode": services.MODE_OF_KIND[voter.kind] != config.access_mode,
        "refusal": services.ineligibility(event, config, voter.user),
        "ballot": ballot,
        "lines": lines,
        "spent": spent,
        "quadratic": config.method == Method.QUADRATIC,
        "via": via,
        "voter_email": voter.link.email if voter.link else "",
        "link_revoked": bool(voter.link and voter.link.revoked_at),
        "open_url": open_url,
        "cast_url": cast_url,
    })


@never_cache
@portal_required("participant")
def vote(request, slug):
    event, config = _voting_event(request, slug)
    return ballot_page(request, event, config, services.Voter(request.user), via="account",
                       open_url=reverse("participant:vote_open", args=[event.slug]),
                       cast_url=reverse("participant:vote_cast", args=[event.slug]))


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
