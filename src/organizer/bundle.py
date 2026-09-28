"""Downloading an event bundle (imports/bundle.py): the organizer page's button and the JSON API.
Two gates (`portal_required("organizer")`, then `get_managed_event`: 404 for anyone who does not
organize the event); the export audits itself. The zip is written to a temporary file, which is
unlinked as soon as it is open, so nothing is left behind whatever happens to the response.

    GET /organizer/events/<slug>/bundle     the button on the event page
    GET /api/events/<slug>/bundle           the same zip, for scripts (Bearer or session)
"""

import os

from django.contrib import messages
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.guards import portal_required
from core import audit
from core.api import error
from events.services import get_managed_event
from imports import bundle


def _zip_response(path, event):
    handle = open(path, "rb")
    os.unlink(path)  # the open handle keeps the data until the response has been sent
    response = FileResponse(handle, content_type="application/zip", as_attachment=True,
                            filename=bundle.filename(event))
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@require_GET
@transaction.non_atomic_requests
@portal_required("organizer")
def bundle_download(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        path = bundle.export_event(event, actor=request.user, origin=audit.origin_of(request))
    except bundle.BundleError as refusal:
        messages.error(request, f"the bundle could not be made: {refusal.detail}")
        return redirect("organizer:event", slug=event.slug)
    return _zip_response(path, event)


@never_cache
@require_GET
@transaction.non_atomic_requests
@portal_required("organizer")
def bundle_api(request, slug):
    event = get_managed_event(request.user, slug)
    try:
        path = bundle.export_event(event, actor=request.user, origin=audit.origin_of(request))
    except bundle.BundleError as refusal:
        return error(refusal.status, refusal.code, refusal.detail)
    return _zip_response(path, event)
