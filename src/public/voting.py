"""Voting by link, no login: /events/<slug>/vote/<token>.

The token is either one allowlisted email's link (email_gated) or the event's open link
(open_link); voting.services.resolve_token tells which, and answers 404 for anything else --
unknown, replaced, or another event's token -- without saying which. The open-link voter is a
random id in a signed cookie, set on the first page view (a cookie, not a database write). Every
rule is voting.services, the same as for logged-in voting.
"""

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from core import audit
from core.net import ip_hash
from events.services import get_visible_event
from participant.voting import ballot_page, lines_from_post
from voting import links, services
from voting.errors import CookieRequired, NoSuchLink, NoVoting, VotingError
from voting.models import Method

COOKIE_MAX_AGE = 60 * 60 * 24 * 60


def _resolve(request, slug, token):
    event = get_visible_event(request.user, slug)
    try:
        kind, link = services.resolve_token(event, token, origin=audit.origin_of(request))
    except (NoSuchLink, NoVoting):
        raise Http404("No such voting link.") from None
    return event, services.voting_for(event), kind, link


def _cookie_voter(request, event):
    return links.cookie_voter_id(event, request.COOKIES.get(links.cookie_name(event)))


def _voter(request, event, kind, link):
    if kind == "link":
        return services.Voter.by_link(link)
    voter_id = _cookie_voter(request, event)
    if not voter_id:
        raise CookieRequired("This browser has no voter cookie yet: open the voting link again, with cookies on.")
    return services.Voter.by_cookie(voter_id, request.user)


def _base(request):
    return request.path.rstrip("/")


@never_cache
def link_vote(request, slug, token):
    event, config, kind, link = _resolve(request, slug, token)
    base = f"/events/{event.slug}/vote/{token}"
    urls = {"open_url": base + "/open", "cast_url": base + "/cast"}
    if kind == "link":
        return ballot_page(request, event, config, services.Voter.by_link(link), via="link", **urls)
    voter_id = _cookie_voter(request, event)
    new_cookie = None
    if not voter_id:
        new_cookie = links.new_cookie_value(event)
        voter_id = links.cookie_voter_id(event, new_cookie)
    response = ballot_page(request, event, config, services.Voter.by_cookie(voter_id, request.user), via="open",
                           **urls)
    if new_cookie:
        response.set_cookie(
            links.cookie_name(event), new_cookie, max_age=COOKIE_MAX_AGE, path=f"/events/{event.slug}/vote/",
            secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Lax",
        )
    return response


def _act(request, slug, token, action):
    event, config, kind, link = _resolve(request, slug, token)
    try:
        voter = _voter(request, event, kind, link)
        action(event, config, voter)
    except VotingError as error:
        messages.error(request, str(error))
    return redirect(f"/events/{event.slug}/vote/{token}")


@require_POST
def link_vote_open(request, slug, token):
    return _act(request, slug, token, lambda event, config, voter: services.open_ballot(
        event, voter, ip_hash=ip_hash(request), origin=audit.origin_of(request)))


@require_POST
def link_vote_cast(request, slug, token):
    def cast(event, config, voter):
        lines = lines_from_post(request.POST, config.method == Method.QUADRATIC)
        services.cast(event, voter, ip_hash(request), lines, voter.user, origin=audit.origin_of(request))
        messages.success(request, "vote saved. you can change it until voting closes.")

    return _act(request, slug, token, cast)
