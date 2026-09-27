"""Results: /organizer/events/<slug>/results.

Compute a preview or the final result, publish or unpublish it, choose who sees it, and download
the winners with their contacts. Every view is gated twice (`portal_required("organizer")`, then
`get_managed_event`). The rules and audit rows are in `scoring.services`; the ranking as shown is
`scoring.results`.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.guards import portal_required
from core import audit
from core.deadlines import db_now
from core.judging import judging_closed
from events.services import get_managed_event
from organizer.export import _download, _stamp, to_csv
from scoring import results as result_views
from scoring import services
from scoring.errors import ScoringError
from scoring.forms import ResultSettingsForm
from scoring.models import ResultSnapshot, SnapshotKind


def _page(request, event, settings_form=None, status=200):
    current = services.result_settings(event)
    snapshots = list(ResultSnapshot.objects.filter(event=event).order_by("-created_at", "-id")[:10])
    latest_final = services.latest_final(event)
    return render(request, "organizer/results.html", {
        "event": event,
        "page": result_views.results_page(event, request.user),
        "settings_form": settings_form or ResultSettingsForm(
            initial={"visibility": current.visibility, "winners_top_n": current.winners_top_n}),
        "snapshots": snapshots,
        "latest_final": latest_final,
        "publication": services.active_publication(event),
        "judging_closed": judging_closed(event, db_now()),
    }, status=status)


@never_cache
@portal_required("organizer")
def results(request, slug):
    event = get_managed_event(request.user, slug)
    return _page(request, event)


@transaction.non_atomic_requests
@require_POST
@portal_required("organizer")
def compute(request, slug):
    """compute_snapshot opens its own REPEATABLE READ transaction, so this view must not run in one."""
    event = get_managed_event(request.user, slug)
    kind = request.POST.get("kind")
    if kind not in SnapshotKind.values:
        messages.error(request, "choose a preview or the final result.")
        return redirect("organizer:results", slug=event.slug)
    try:
        snapshot = services.compute_snapshot(event, kind, actor=request.user, request=request)
    except (ScoringError, PermissionDenied) as error:
        messages.error(request, str(error))
        return redirect("organizer:results", slug=event.slug)
    messages.success(request, f"{kind} #{snapshot.pk} computed.")
    return redirect("organizer:results", slug=event.slug)


@require_POST
@portal_required("organizer")
def publish(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        snapshot_id = int(request.POST.get("snapshot", ""))
    except ValueError:
        messages.error(request, "choose the final result to publish.")
        return redirect("organizer:results", slug=event.slug)
    try:
        services.publish_results(event, snapshot_id, actor=request.user, origin=audit.origin_of(request))
    except (ScoringError, PermissionDenied) as error:
        messages.error(request, str(error))
        return redirect("organizer:results", slug=event.slug)
    messages.success(request, "results published.")
    return redirect("organizer:results", slug=event.slug)


@require_POST
@portal_required("organizer")
def unpublish(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        services.unpublish_results(event, actor=request.user, origin=audit.origin_of(request))
    except (ScoringError, PermissionDenied) as error:
        messages.error(request, str(error))
        return redirect("organizer:results", slug=event.slug)
    messages.success(request, "results unpublished: only organizers can see them now.")
    return redirect("organizer:results", slug=event.slug)


@require_POST
@portal_required("organizer")
def settings(request, slug):
    event = get_managed_event(request.user, slug)
    form = ResultSettingsForm(request.POST)
    if not form.is_valid():
        return _page(request, event, settings_form=form, status=400)
    try:
        services.set_result_settings(
            event, actor=request.user, origin=audit.origin_of(request),
            visibility=form.cleaned_data["visibility"], winners_top_n=form.cleaned_data["winners_top_n"])
    except ScoringError as error:
        messages.error(request, str(error))
        return redirect("organizer:results", slug=event.slug)
    messages.success(request, "result visibility saved.")
    return redirect("organizer:results", slug=event.slug)


@never_cache
@require_GET
@portal_required("organizer")
def winners_csv(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        snapshot, rows = result_views.winners_rows(event, actor=request.user, origin=audit.origin_of(request))
    except ScoringError as error:
        messages.error(request, str(error))
        return redirect("organizer:results", slug=event.slug)
    return _download(to_csv(result_views.WINNERS_HEADER, rows), "text/csv; charset=utf-8",
                     f"{event.slug}-winners-{_stamp(db_now())}.csv")
