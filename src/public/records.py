"""Public pages for signed records (C2): the printable certificate, its JSON, /verify, and the
.well-known lists. Anyone may read them; the only write is /verify's audit row (a rate-limited POST).

    GET  /records/<uuid>                               the certificate, with a server-side check
    GET  /records/<uuid>.json                          {payload, payload_text, signature, kid, alg}
    GET  /verify   POST /verify                        paste a payload + signature
    GET  /.well-known/dogfood-signing-keys.json        this install's keys, retired ones too
    GET  /.well-known/dogfood-foreign-signing-keys.json  keys of other installs (imported records)
    GET  /.well-known/dogfood-revoked.json             sha256 of each revoked record's id

A record's id (its uuid) is also its certificate's address: whoever has it can open the page. So the
revoked list never names ids, only sha256(id) -- the id in its usual text form (lower-case hex with
hyphens), UTF-8, hashed, hex. A verifier holding a record hashes its record_id and looks it up. Public
pages show why a record was revoked only as a category (superseded, with a link to the newer record;
no longer eligible; revoked by the organizer); the organizer's own words stay with the organizers and
in the audit log.
"""

import hashlib

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
    return render(request, "public/record.html", {"record": record, "verdict": verdict,
                                                  "replacement": services.replacement(record)})


@never_cache
@require_GET
def record_json(request, record_id):
    record = _record(record_id)
    replaced_by = services.replacement(record)
    revocation = None if record.revoked_at is None else {
        "revoked_at": _iso(record.revoked_at), "category": record.revoke_category,
        "replaced_by": str(replaced_by.pk) if replaced_by else None}
    response = JsonResponse({"payload": record.payload, "payload_text": record.payload_text,
                             "signature": record.signature, "kid": record.kid, "alg": ED25519,
                             "revoked": record.revoked_at is not None, "revocation": revocation})
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
    return JsonResponse({
        "hash": "sha256 of the record_id's text (lower-case, with hyphens), hex",
        "revoked": sorted(({"record_id_sha256": revoked_id_hash(r.pk), "revoked_at": _iso(r.revoked_at)}
                           for r in rows), key=lambda r: (r["revoked_at"], r["record_id_sha256"])),
    })


def revoked_id_hash(record_id):
    return hashlib.sha256(str(record_id).lower().encode("utf-8")).hexdigest()
