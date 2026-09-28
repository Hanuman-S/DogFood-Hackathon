"""The organizer's Records page: issue signed records per kind, see them, revoke one (a reason is
required). Two gates on every view; the rules are records/services.py's. When this install cannot
sign, the page says why and what to do (the banner in _signing_banner.html)."""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from core import audit
from events.services import get_managed_event
from records import keys, services
from records.errors import RecordError
from records.models import IssuedRecord, RecordKind


@never_cache
@portal_required("organizer")
def records_page(request, slug):
    event = get_managed_event(request.user, slug)
    rows = (IssuedRecord.objects.filter(event=event).select_related("subject_user", "revoked_by")
            .order_by("revoked_at", "kind", "subject_user__name", "slot"))
    state, kid = keys.status()
    return render(request, "organizer/records.html", {
        "event": event, "records": rows, "kinds": RecordKind.choices, "signing_state": state, "signing_kid": kid,
    })


@require_POST
@portal_required("organizer")
def records_issue(request, slug):
    event = get_managed_event(request.user, slug)
    kind = request.POST.get("kind", "")
    try:
        counts = services.issue_records(event, kind, actor=request.user, origin=audit.origin_of(request))
    except RecordError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"{kind}: {counts['issued']} issued, {counts['reissued']} reissued, "
                                  f"{counts['unchanged']} unchanged, {counts['revoked']} revoked (no longer eligible).")
    return redirect("organizer:records", slug=event.slug)


@require_POST
@portal_required("organizer")
def record_revoke(request, slug, record_id):
    event = get_managed_event(request.user, slug)
    try:
        services.revoke_record(record_id, request.POST.get("reason", ""), actor=request.user,
                               origin=audit.origin_of(request))
    except RecordError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "record revoked. it stays public, marked revoked.")
    return redirect("organizer:records", slug=event.slug)
