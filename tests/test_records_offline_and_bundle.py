"""C2c: the offline verifier (scripts/verify_record.py: pure Python, RFC 8032 section 7.1 vectors, the
hashed revoked list) and signed records travelling in an event bundle."""

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client

from accounts.models import User
from accounts.roles import Role
from imports import bundle, bundle_import
from records import keys, services
from records.models import ForeignSigningKey, IssuedRecord, RecordKind, SigningKey
from test_results_flow import judged, publish  # noqa: F401 -- fixtures and helpers

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_record.py"
sys.path.insert(0, str(SCRIPT.parent))
import verify_record  # noqa: E402

TX = pytest.mark.django_db(transaction=True)

# RFC 8032, section 7.1: (secret key, public key, message, signature), all hex.
VECTORS = {
    "TEST 1": ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
               "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
               "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
               "d25bf5f0595bbe24655141438e7a100b"),
    "TEST 2": ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
               "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
               "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
               "387b2eaeb4302aeeb00d291612bb0c00"),
    "TEST 3": ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
               "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
               "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc659"
               "4a7c15e9716ed28dc027beceea1ec40a"),
    "TEST SHA(abc)": ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
                      "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
                      "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd"
                      "454d4423643ce80e2a9ac94fa54ca49f",
                      "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e030"
                      "3dca179c138ac17ad9bef1177331a704"),
}


# --- the vendored verifier against RFC 8032 -----------------------------------------------------------------

@pytest.mark.parametrize("name", list(VECTORS))
def test_rfc8032_vectors_verify(name):
    secret, public, message, signature = (bytes.fromhex(v) for v in VECTORS[name])
    assert verify_record.ed25519_verify(public, message, signature)
    # the vector itself is right: an independent implementation derives the same key and signature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(secret)
    assert keys.raw_public(key) == public and key.sign(message) == signature


@pytest.mark.parametrize("name", list(VECTORS))
def test_rfc8032_vectors_fail_when_anything_changes(name):
    _, public, message, signature = (bytes.fromhex(v) for v in VECTORS[name])
    assert not verify_record.ed25519_verify(public, message + b"x", signature)
    assert not verify_record.ed25519_verify(public, message, signature[:-1] + bytes([signature[-1] ^ 1]))
    other = bytes.fromhex(VECTORS["TEST 1" if name != "TEST 1" else "TEST 2"][1])
    assert not verify_record.ed25519_verify(other, message, signature)


# --- the script on records the app made -------------------------------------------------------------------

def run_script(tmp_path, record, keys_json, revoked=None):
    (tmp_path / "record.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / "keys.json").write_text(json.dumps(keys_json), encoding="utf-8")
    args = [sys.executable, str(SCRIPT), str(tmp_path / "record.json"), str(tmp_path / "keys.json")]
    if revoked is not None:
        (tmp_path / "revoked.json").write_text(json.dumps(revoked), encoding="utf-8")
        args += ["--revoked", str(tmp_path / "revoked.json")]
    done = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture
def signing(settings, tmp_path):
    settings.SIGNING_KEY_DIR = tmp_path / "signing"
    keys.ensure_signing_key()


@TX
def test_the_offline_script_verifies_a_record_the_app_issued(judged, signing, tmp_path):
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    record = IssuedRecord.objects.filter(event=judged).first()
    downloaded = Client().get(f"/records/{record.pk}.json").json()
    published = Client().get("/.well-known/dogfood-signing-keys.json").json()
    status, out = run_script(tmp_path, downloaded, published)
    assert status == 0 and "valid" in out, out
    status, _ = run_script(tmp_path, downloaded, {"kid": record.kid, "public_key": published["keys"][0]["public_key"]})
    assert status == 0
    tampered = dict(downloaded, payload_text=downloaded["payload_text"].replace('"reviews_submitted":4',
                                                                                  '"reviews_submitted":9'))
    status, out = run_script(tmp_path, tampered, published)
    assert status == 1 and "invalid" in out
    status, out = run_script(tmp_path, downloaded, {"keys": []})
    assert status == 1 and "unknown key" in out
    clean = Client().get("/.well-known/dogfood-revoked.json").json()
    assert run_script(tmp_path, downloaded, published, clean)[0] == 0
    services.revoke_record(record.pk, "test", actor=judged.organizer)
    revoked = Client().get("/.well-known/dogfood-revoked.json").json()
    status, out = run_script(tmp_path, downloaded, published, revoked)
    assert status == 2 and "revoked" in out  # the hashed list: the script hashes the record_id itself
    assert run_script(tmp_path, {"no": "fields"}, published)[0] == 1


# --- records and keys in a bundle -------------------------------------------------------------------------

def export(event):
    path = bundle.export_event(event, actor=None)
    try:
        return open(path, "rb").read()
    finally:
        os.unlink(path)


def import_as(data, tmp_path, actor):
    path = tmp_path / "b.zip"
    path.write_bytes(data)
    return bundle_import.import_event(str(path), actor=actor)


@TX
def test_the_bundle_carries_records_and_public_keys_never_a_private_one(judged, signing):
    from cryptography.hazmat.primitives import serialization

    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    keys.rotate()
    services.issue_records(judged, RecordKind.PARTICIPANT, actor=judged.organizer)
    data = export(judged)
    files = {i.filename: zipfile.ZipFile(io.BytesIO(data)).read(i.filename)
             for i in zipfile.ZipFile(io.BytesIO(data)).infolist()}
    body = json.loads(files["event.json"])
    assert len(body["issued_records"]) == 7
    assert {r["record_id"] for r in body["issued_records"]} == {str(pk) for pk in IssuedRecord.objects.values_list("pk", flat=True)}
    assert {k["kid"] for k in body["signing_keys"]} == set(SigningKey.objects.values_list("kid", flat=True))
    blob = b"\n".join(files.values())
    assert b"PRIVATE KEY" not in blob and b"BEGIN" not in blob
    for kid in SigningKey.objects.values_list("kid", flat=True):
        pem = open(keys.pem_path(kid), "rb").read()
        private = serialization.load_pem_private_key(pem, password=None)
        raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
        import base64
        for form in (raw, raw.hex().encode(), base64.b64encode(raw), pem.split(b"\n")[1]):
            assert form not in blob, kid


@TX
def test_an_admin_import_on_a_fresh_install_brings_records_and_their_keys_as_foreign(judged, signing, tmp_path):
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    ids = set(IssuedRecord.objects.values_list("pk", flat=True))
    data = export(judged)
    source_kid = SigningKey.objects.get().kid
    call_command("flush", interactive=False, verbosity=0)
    keys.ensure_signing_key()  # the new install's own key
    own_kid = SigningKey.objects.get().kid
    admin = User.objects.create_user("admin@example.org", None, name="Admin", is_platform_admin=True)
    event = import_as(data, tmp_path, admin)
    assert set(IssuedRecord.objects.filter(event=event).values_list("pk", flat=True)) == ids
    assert all(r.is_foreign for r in IssuedRecord.objects.filter(event=event))
    assert ForeignSigningKey.objects.get().kid == source_kid
    own = {k["kid"] for k in Client().get("/.well-known/dogfood-signing-keys.json").json()["keys"]}
    assert own == {own_kid}  # a foreign key is never listed as this install's own
    foreign = {k["kid"] for k in Client().get("/.well-known/dogfood-foreign-signing-keys.json").json()["keys"]}
    assert foreign == {source_kid}
    record = IssuedRecord.objects.filter(event=event).first()
    assert services.verify_record(record).state == "foreign_valid"
    assert f"signed by another install (kid {source_kid})" in Client().get(f"/records/{record.pk}").content.decode()


@TX
def test_a_same_install_import_keeps_own_keys_own_and_skips_existing_records(judged, signing, tmp_path, make_user):
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    data = export(judged)
    event = import_as(data, tmp_path, make_user(role="admin", email="another.admin@example.org"))
    assert not ForeignSigningKey.objects.exists()
    assert not IssuedRecord.objects.filter(event=event).exists()
    from core.models import AuditAction, AuditLog
    detail = AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED, subject=event.slug).detail
    assert detail["records"]["skipped_existing"] == 3 and detail["records"]["imported"] == 0


@TX
def test_an_event_creators_import_skips_records_and_keys(judged, signing, tmp_path, make_user):
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    data = export(judged)
    creator = make_user(role=Role.ORGANIZER, email="creator@example.org")
    event = import_as(data, tmp_path, creator)
    assert not IssuedRecord.objects.filter(event=event).exists() and not ForeignSigningKey.objects.exists()
    from core.models import AuditAction, AuditLog
    detail = AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED, subject=event.slug).detail
    assert detail["records"]["skipped_not_admin"] == 3


@TX
def test_a_bundle_with_a_tampered_record_is_refused(judged, signing, tmp_path, make_user):
    from test_bundle_import_validation import edit_body, rezip

    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    data = export(judged)
    files = {i.filename: zipfile.ZipFile(io.BytesIO(data)).read(i.filename)
             for i in zipfile.ZipFile(io.BytesIO(data)).infolist()}

    def tamper(body):
        record = body["issued_records"][0]
        record["payload"]["reviews_submitted"] = 99
        from records.canonical import canonical
        record["payload_text"] = canonical(record["payload"]).decode()
    with pytest.raises(bundle.BundleError) as caught:
        import_as(rezip(edit_body(files, tamper)), tmp_path, make_user(role="admin", email="a2@example.org"))
    assert caught.value.code == "invalid_bundle" and "does not verify" in caught.value.detail
