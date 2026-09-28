"""Downloading an event bundle (imports/bundle.py): the organizer page's button and the JSON API.
Two gates (`portal_required("organizer")`, then `get_managed_event`: 404 for anyone who does not
organize the event); the export audits itself. The zip is written to a temporary file, which is
unlinked as soon as it is open, so nothing is left behind whatever happens to the response.

    GET /organizer/events/<slug>/bundle     the button on the event page
    GET /api/events/<slug>/bundle           the same zip, for scripts (Bearer or session)
    GET/POST /organizer/events/import       import a bundle as a new event (admins; event creators)
    POST /api/bundles                       the same, for scripts (multipart field "bundle")
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


# --- importing -------------------------------------------------------------------------------------

def _import(request):
    """Save the upload (capped while copying), import it, and always remove the temporary file."""
    from imports import bundle_import, bundle_validate

    from core.models import AuditAction

    try:
        upload = request.FILES.get("bundle")
        if upload is None:
            raise bundle.BundleError("no_file", "choose a bundle (.zip) to import")
        path = bundle_validate.save_upload(upload)
    except bundle.BundleError as refusal:  # refused before the service: audited here, like every refusal
        audit.record(AuditAction.EVENT_IMPORT_REFUSED, request=request, reason=refusal.code,
                     detail_text=refusal.detail[:300])
        raise
    try:
        return bundle_import.import_event(path, actor=request.user, origin=audit.origin_of(request))
    finally:
        os.unlink(path)


@never_cache
@portal_required("organizer")
def import_page(request):
    """GET: the upload form. POST: import it as a new, unpublished event of which you are an organizer."""
    from django.shortcuts import render

    from imports.bundle_validate import ImportForbidden, may_import

    if not may_import(request.user):
        from core.views import forbidden
        return forbidden(request, reason="Only platform admins and accounts that may create events can import one.")
    if request.method == "POST":
        try:
            event = _import(request)
        except (bundle.BundleError, ImportForbidden) as refusal:
            return render(request, "organizer/import.html", {"error": refusal.detail}, status=refusal.status)
        messages.success(request, f"imported as {event.slug}. it is unpublished: check it, then publish it.")
        return redirect("organizer:event", slug=event.slug)
    return render(request, "organizer/import.html", {})


@never_cache
@portal_required("organizer")
def import_api(request):
    """POST /api/bundles (multipart, field "bundle"): 201 with the new event's slug."""
    from django.http import JsonResponse

    from imports.bundle_validate import ImportForbidden

    if request.method != "POST":
        return error(405, "method_not_allowed", "POST a bundle as multipart field 'bundle'.")
    try:
        event = _import(request)
    except (bundle.BundleError, ImportForbidden) as refusal:
        return error(refusal.status, refusal.code, refusal.detail)
    return JsonResponse({"slug": event.slug, "name": event.name, "published": event.is_published}, status=201)
