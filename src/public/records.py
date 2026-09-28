"""Public pages for signed records (C2): the printable certificate, its JSON, /verify, and the
.well-known lists. Anyone may read them; the only write is /verify's audit row (a rate-limited POST).

    GET  /records/<uuid>                               the certificate, with a server-side check
    GET  /records/<uuid>.json                          {payload, payload_text, signature, kid, alg}
    GET  /verify   POST /verify                        paste a payload + signature
    GET  /.well-known/dogfood-signing-keys.json        this install's keys, retired ones too
    GET  /.well-known/dogfood-foreign-signing-keys.json  keys of other installs (imported records)
    GET  /.well-known/dogfood-revoked.json             revoked record ids
"""

import json

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core import audit
from records import services
from records.errors import RateLimited
from records.models import ED25519, ForeignSigningKey, IssuedRecord, SigningKey


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def _record(record_id):
    record = IssuedRecord.objects.select_related("event", "subject_user").filter(pk=record_id).first()
    if record is None:
        raise Http404("No such record.")
    return record


@never_cache
@require_GET
def record_page(request, record_id):
    record = _record(record_id)
    verdict = services.verify_record(record)
    pretty = json.dumps(record.payload, indent=2, sort_keys=True, ensure_ascii=False)
    return render(request, "public/record.html", {"record": record, "verdict": verdict, "pretty": pretty})


@never_cache
@require_GET
def record_json(request, record_id):
    record = _record(record_id)
    response = JsonResponse({"payload": record.payload, "payload_text": record.payload_text,
                             "signature": record.signature, "kid": record.kid, "alg": ED25519,
                             "revoked": record.revoked_at is not None})
    response["Content-Disposition"] = f'attachment; filename="record-{record.pk}.json"'
    return response


@never_cache
@require_http_methods(["GET", "POST"])
def verify(request):
    context = {"payload": "", "signature": ""}
    status = 200
    if request.method == "POST":
        context["payload"] = request.POST.get("payload", "")[:20000]
        context["signature"] = request.POST.get("signature", "")[:200]
        try:
            context["verdict"] = services.verify_submission(context["payload"], context["signature"],
                                                            origin=audit.origin_of(request))
        except RateLimited as error:
            context["error"], status = error.detail, error.status
    return render(request, "public/verify.html", context, status=status)


def _keys(rows):
    return [{"kid": k.kid, "alg": k.alg, "public_key": k.public_key, "created_at": _iso(k.created_at),
             "retired_at": _iso(k.retired_at)} for k in rows]


@never_cache
@require_GET
def signing_keys(request):
    """This install's own keys only (retired ones too: their records keep verifying)."""
    return JsonResponse({"keys": _keys(SigningKey.objects.order_by("created_at", "kid"))})


@never_cache
@require_GET
def foreign_signing_keys(request):
    """Keys of other installs, which came with imported events. Published apart from this install's own,
    so nobody mistakes them for keys this install signs with."""
    rows = ForeignSigningKey.objects.order_by("imported_at", "kid")
    return JsonResponse({"keys": [dict(k, imported_from=f.imported_from)
                                  for k, f in zip(_keys(rows), rows)]})


@never_cache
@require_GET
def revoked(request):
    rows = IssuedRecord.objects.filter(revoked_at__isnull=False).order_by("revoked_at", "id")
    return JsonResponse({"revoked": [{"id": str(r.pk), "revoked_at": _iso(r.revoked_at)} for r in rows]})
