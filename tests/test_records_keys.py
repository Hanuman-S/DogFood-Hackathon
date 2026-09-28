"""C2a: canonical payload bytes, this install's signing keys (files, the active row, boot, rotation),
and the immutability of issued records and keys (Postgres triggers)."""

import base64
import hashlib
import os
import stat
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from core.models import AuditAction, AuditLog
from records import keys
from records.canonical import CanonicalError, canonical
from records.models import IssuedRecord, RecordKind, SigningKey, winner_slot

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def key_dir(settings, tmp_path):
    settings.SIGNING_KEY_DIR = tmp_path / "signing"
    return settings.SIGNING_KEY_DIR


# --- canonical --------------------------------------------------------------------------------------

def test_canonical_is_sorted_compact_utf8():
    payload = {"b": [2, "x"], "a": {"z": "é", "y": 1}, "c": ""}
    assert canonical(payload) == '{"a":{"y":1,"z":"é"},"b":[2,"x"],"c":""}'.encode("utf-8")


@pytest.mark.parametrize("value", [1.5, True, False, None, {"a": [1, 2.0]}, {"a": {"b": None}}, [True],
                                   2 ** 53, (1, 2), {1: "numeric key"}, {"s": {1, 2}}])
def test_canonical_refuses_what_another_verifier_could_write_differently(value):
    with pytest.raises(CanonicalError):
        canonical(value)


# --- keys: boot ------------------------------------------------------------------------------------------

def test_the_first_boot_makes_a_key_file_0600_in_a_0700_folder_and_its_row(key_dir):
    state, kid = keys.ensure_signing_key()
    assert state == "created"
    path = keys.pem_path(kid)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(key_dir).st_mode) == 0o700
    row = SigningKey.objects.get()
    assert row.kid == kid and row.retired_at is None
    raw = base64.b64decode(row.public_key)
    assert len(raw) == 32 and kid == hashlib.sha256(raw).hexdigest()[:16]
    assert AuditLog.objects.filter(action=AuditAction.SIGNING_KEY_CREATED, subject=kid).exists()


def test_a_second_boot_changes_nothing():
    _, kid = keys.ensure_signing_key()
    assert keys.ensure_signing_key() == ("ok", kid)
    assert SigningKey.objects.count() == 1 and len(os.listdir(keys.key_dir())) == 1


def test_a_stray_pem_with_no_row_is_never_adopted(key_dir):
    os.makedirs(key_dir, exist_ok=True)
    stray = keys._new_key()
    stray_kid, _ = keys._write_pem(stray)
    state, kid = keys.ensure_signing_key()
    assert state == "created" and kid != stray_kid
    assert SigningKey.objects.get().kid == kid


def test_a_missing_or_wrong_file_is_reported_signing_refused_and_no_key_made():
    _, kid = keys.ensure_signing_key()
    os.unlink(keys.pem_path(kid))
    assert keys.ensure_signing_key() == ("missing", kid)
    with pytest.raises(keys.SigningUnavailable):
        keys.sign(b"x")
    other = keys._new_key()
    from cryptography.hazmat.primitives import serialization
    with open(keys.pem_path(kid), "wb") as fh:  # another key's PEM under the active kid's name
        fh.write(other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    assert keys.ensure_signing_key() == ("mismatch", kid)
    assert SigningKey.objects.count() == 1


def test_the_boot_command_reports_a_missing_file_but_does_not_fail():
    _, kid = keys.ensure_signing_key()
    os.unlink(keys.pem_path(kid))
    out, err = StringIO(), StringIO()
    call_command("ensure_signing_key", stdout=out, stderr=err)
    assert "SIGNING KEY PROBLEM" in err.getvalue() and kid in err.getvalue()


# --- signing, rotation ------------------------------------------------------------------------------------

def test_sign_and_verify():
    keys.ensure_signing_key()
    kid, signature = keys.sign(b"payload")
    public = SigningKey.objects.get(kid=kid).public_key
    assert keys.verify(public, b"payload", signature)
    assert not keys.verify(public, b"payloaD", signature)
    assert not keys.verify(public, b"payload", base64.b64encode(b"\0" * 64).decode())
    assert not keys.verify("not base64!", b"payload", signature)


def test_rotation_retires_the_old_key_keeps_it_published_and_old_signatures_verify():
    _, old = keys.ensure_signing_key()
    _, old_signature = keys.sign(b"before")
    retired, new = keys.rotate()
    assert retired == old and new != old
    rows = {r.kid: r for r in SigningKey.objects.all()}
    assert rows[old].retired_at is not None and rows[new].retired_at is None
    assert SigningKey.objects.filter(retired_at__isnull=True).count() == 1
    assert os.path.exists(keys.pem_path(old))  # kept on disk, never used again
    assert keys.verify(rows[old].public_key, b"before", old_signature)
    kid, _ = keys.sign(b"after")
    assert kid == new
    assert AuditLog.objects.get(action=AuditAction.SIGNING_KEY_ROTATED).detail == {"old": old, "new": new}


def test_a_failed_rotation_leaves_the_old_key_active_and_no_new_file(monkeypatch):
    _, old = keys.ensure_signing_key()
    files = set(os.listdir(keys.key_dir()))

    def fail(**kwargs):
        raise IntegrityError("forced")
    monkeypatch.setattr(SigningKey.objects, "create", fail)
    with pytest.raises(IntegrityError):
        keys.rotate()
    assert SigningKey.objects.get().kid == old and SigningKey.objects.get().retired_at is None
    assert set(os.listdir(keys.key_dir())) == files


def test_the_rotate_command():
    keys.ensure_signing_key()
    out = StringIO()
    call_command("rotate_signing_key", stdout=out)
    assert "signing key rotated" in out.getvalue() and SigningKey.objects.count() == 2


def test_the_database_allows_one_active_key_only():
    SigningKey.objects.create(kid="a" * 16, public_key="x")
    with pytest.raises(IntegrityError), transaction.atomic():
        SigningKey.objects.create(kid="b" * 16, public_key="y")


def test_a_signing_key_can_only_be_retired_once_and_never_deleted():
    key = SigningKey.objects.create(kid="c" * 16, public_key="x")
    with pytest.raises(DatabaseError), transaction.atomic():
        SigningKey.objects.filter(pk=key.pk).update(public_key="changed")
    with pytest.raises(DatabaseError), transaction.atomic():
        SigningKey.objects.filter(pk=key.pk).delete()
    SigningKey.objects.filter(pk=key.pk).update(retired_at=timezone.now())
    with pytest.raises(DatabaseError), transaction.atomic():
        SigningKey.objects.filter(pk=key.pk).update(retired_at=None)


# --- issued records: immutable except revocation ------------------------------------------------------------

@pytest.fixture
def record(make_event, make_user):
    event = make_event()
    return IssuedRecord.objects.create(kind=RecordKind.PARTICIPANT, event=event, subject_user=make_user(),
                                       payload={"v": 1}, payload_text='{"v":1}', signature="s", kid="k" * 16)


def test_a_record_cannot_be_changed_or_deleted(record):
    for change in ({"payload_text": '{"v":2}'}, {"kind": RecordKind.WINNER}, {"signature": "t"},
                   {"issued_at": timezone.now()}):
        with pytest.raises(DatabaseError), transaction.atomic():
            IssuedRecord.objects.filter(pk=record.pk).update(**change)
    with pytest.raises(DatabaseError), transaction.atomic():
        IssuedRecord.objects.filter(pk=record.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():  # revoking without a reason
        IssuedRecord.objects.filter(pk=record.pk).update(revoked_at=timezone.now())


def test_a_record_is_revoked_once_and_never_unrevoked(record):
    IssuedRecord.objects.filter(pk=record.pk).update(revoked_at=timezone.now(), revoke_reason="issued in error")
    for change in ({"revoked_at": None, "revoke_reason": ""}, {"revoke_reason": "another reason"}):
        with pytest.raises(DatabaseError), transaction.atomic():
            IssuedRecord.objects.filter(pk=record.pk).update(**change)


def test_an_event_with_issued_records_cannot_be_deleted(record):
    from core.deadlines import deadline_bypass
    with pytest.raises(ProtectedError), deadline_bypass(None, "test: try to delete the event"):
        record.event.delete()


def test_one_active_record_per_slot_and_winner_slots_differ_by_track_place_and_peoples_choice(record):
    same = dict(kind=record.kind, event=record.event, subject_user=record.subject_user, payload={},
                payload_text="{}", signature="s", kid="k" * 16)
    with pytest.raises(IntegrityError), transaction.atomic():
        IssuedRecord.objects.create(**same)
    IssuedRecord.objects.filter(pk=record.pk).update(revoked_at=timezone.now(), revoke_reason="reissued")
    IssuedRecord.objects.create(**same)  # the slot is free again once the old one is revoked
    slots = {winner_slot("", 1, False), winner_slot("Hardware", 1, False), winner_slot("", 1, True),
             winner_slot("", 2, False)}
    assert len(slots) == 4
    winners = dict(same, kind=RecordKind.WINNER)
    for slot in slots:
        IssuedRecord.objects.create(**winners, slot=slot)
    with pytest.raises(IntegrityError), transaction.atomic():
        IssuedRecord.objects.create(**winners, slot=winner_slot("Hardware", 1, False))


# --- review items: both key tables, no caching, negative integers --------------------------------------

@pytest.mark.parametrize("model_name", ["SigningKey", "ForeignSigningKey"])
def test_neither_key_table_lets_public_key_or_kid_change(model_name):
    from records import models as m
    model = getattr(m, model_name)
    extra = {"imported_from": "a" * 64} if model_name == "ForeignSigningKey" else {}
    key = model.objects.create(kid="d" * 16, public_key="original", **extra)
    for change in ({"public_key": "swapped"}, {"kid": "e" * 16}, {"alg": "RSA"},
                   {"public_key": "swapped", "retired_at": timezone.now()}):
        with pytest.raises(DatabaseError), transaction.atomic():
            model.objects.filter(pk=key.pk).update(**change)
    with pytest.raises(DatabaseError), transaction.atomic():
        model.objects.filter(pk=key.pk).delete()
    model.objects.filter(pk=key.pk).update(retired_at=timezone.now())  # the one change allowed
    with pytest.raises(DatabaseError), transaction.atomic():
        model.objects.filter(pk=key.pk).update(retired_at=timezone.now())  # and only once
    assert model.objects.get(pk=key.pk).public_key == "original"


def test_signing_right_after_a_rotation_uses_the_new_key_with_no_restart():
    keys.ensure_signing_key()
    first, _ = keys.sign(b"a")
    _, new = keys.rotate()
    second, signature = keys.sign(b"b")
    assert second == new != first
    assert keys.verify(SigningKey.objects.get(kid=new).public_key, b"b", signature)
    call_command("rotate_signing_key", stdout=StringIO())
    third, _ = keys.sign(b"c")
    assert third == SigningKey.objects.get(retired_at__isnull=True).kid not in (first, second)


def test_canonical_refuses_integers_beyond_the_exact_range_either_side():
    assert canonical([2 ** 53 - 1, -(2 ** 53 - 1)]) == b"[9007199254740991,-9007199254740991]"
    for value in (2 ** 53, -(2 ** 53), -(2 ** 63)):
        with pytest.raises(CanonicalError):
            canonical({"n": value})
