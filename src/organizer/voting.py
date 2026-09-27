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
from voting import integrity as evidence
from voting import links
from voting.models import Method, VoterLink

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
        "voter_links": [(link, f"{_base_url(request)}/events/{event.slug}/vote/{links.link_token(link)}")
                        for link in VoterLink.objects.filter(event=event).order_by("email")] if config else [],
        "open_link": (f"{_base_url(request)}/events/{event.slug}/vote/{links.open_token(config)}"
                      if config and config.access_mode == "open_link" and config.open_link_nonce else ""),
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


# --- email links and the open link ------------------------------------------------------------------

def _base_url(request):
    return request.build_absolute_uri("/").rstrip("/")


@require_POST
@portal_required("organizer")
def voter_links_add(request, slug):
    event = get_managed_event(request.user, slug)
    upload = request.FILES.get("csv_file")
    if upload is not None and upload.size > 2 * 1024 * 1024:
        messages.error(request, "the file is too large (2 MB at most).")
        return redirect("organizer:voting", slug=event.slug)
    try:
        created, reissued, unchanged, rejected = services.add_voter_links(
            event, actor=request.user, origin=audit.origin_of(request), text=request.POST.get("emails", ""),
            csv_bytes=upload.read() if upload is not None else b"")
    except VotingError as error:
        messages.error(request, str(error))
    else:
        note = f"{len(created)} link{'s' if len(created) != 1 else ''} created"
        if reissued:
            note += f", {len(reissued)} revoked link{'s' if len(reissued) != 1 else ''} reissued"
        if unchanged:
            note += f", {len(unchanged)} already allowlisted"
        if rejected:
            note += f"; not an email, skipped: {', '.join(rejected[:5])}{' ...' if len(rejected) > 5 else ''}"
        messages.success(request, note + ".")
    return redirect("organizer:voting", slug=event.slug)


@require_POST
@portal_required("organizer")
def voter_link_revoke(request, slug, link_id):
    event = get_managed_event(request.user, slug)
    try:
        link = services.revoke_voter_link(event, link_id, actor=request.user, origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"link for {link.email} revoked.")
    return redirect("organizer:voting", slug=event.slug)


@never_cache
@require_GET
@portal_required("organizer")
def voter_links_csv(request, slug):
    event = get_managed_event(request.user, slug)
    rows = services.voter_links_rows(event, actor=request.user, base_url=_base_url(request),
                                     origin=audit.origin_of(request))
    return download(to_csv(services.VOTER_LINKS_HEADER, rows), "text/csv; charset=utf-8",
                    f"{event.slug}-voter-links-{stamp(db_now())}.csv")


@require_POST
@portal_required("organizer")
def open_link_rotate(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        services.rotate_open_link(event, actor=request.user, origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "new open link made; the old one no longer works.")
    return redirect("organizer:voting", slug=event.slug)


# --- integrity -------------------------------------------------------------------------------------

@never_cache
@portal_required("organizer")
def integrity(request, slug):
    """Flags, voided ballots, the audit trail and the position profile. Read-only (voting.integrity)."""
    event = get_managed_event(request.user, slug)
    return render(request, "organizer/voting_integrity.html", {
        "event": event,
        "config": services.voting_for(event),
        "counts": evidence.counts(event),
        "flags": [(flag, evidence.ballot_rows(flag.ballots)) for flag in evidence.flags(event)],
        "voided": evidence.ballot_rows(evidence.voided(event)),
        "positions": evidence.position_bias(event),
        "trail": evidence.trail(event),
    })


@require_POST
@portal_required("organizer")
def ballot_void(request, slug, ballot_id):
    event = get_managed_event(request.user, slug)
    try:
        services.void_ballot(event, ballot_id, actor=request.user, reason=request.POST.get("reason", ""),
                             origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"ballot #{ballot_id} voided. it is kept, and left out of every tally from now on.")
    return redirect("organizer:voting_integrity", slug=event.slug)


@require_POST
@portal_required("organizer")
def ballot_restore(request, slug, ballot_id):
    event = get_managed_event(request.user, slug)
    try:
        services.restore_ballot(event, ballot_id, actor=request.user, reason=request.POST.get("reason", ""),
                                origin=audit.origin_of(request))
    except VotingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"ballot #{ballot_id} restored. it counts again from the next tally.")
    return redirect("organizer:voting_integrity", slug=event.slug)
