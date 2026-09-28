"""This install's Ed25519 signing keys: the private halves as PEM files in the secrets volume, the public
halves as SigningKey rows.

Files. `<SIGNING_KEY_DIR>/<kid>.pem`, PKCS8, unencrypted (the volume is the protection, as for
SECRET_KEY), created with O_EXCL and mode 0600 in a directory made 0700 -- like config/secret_key.py.
In the compose setup the directory is inside the `secrets` named volume (a Linux filesystem even on
Docker Desktop for Windows), never the repo or the image.

kid = the first 16 hex characters of sha256(the 32-byte raw public key).

Which key signs is decided by the database, never by which files exist: the active key is the one
SigningKey row with retired_at IS NULL, and its private half is `<kid>.pem`. At boot,
`ensure_signing_key`:
* no active row -> make a key: write the PEM, then insert the row (a stray PEM with no row is never
  adopted: nothing says it was ever this install's);
* an active row whose PEM is there and matches -> nothing to do;
* an active row whose PEM is missing or does not match -> say so loudly and carry on: signing is then
  refused (503 signing_unavailable) until the operator runs `rotate_signing_key`. Old records still
  verify: their public keys are in the database.

Rotation writes the new PEM first, then in ONE transaction retires the active row (first) and inserts
the new one; if that fails the new PEM is removed. The retired key stays published.
"""

import base64
import hashlib
import os

from django.conf import settings
from django.db import connection, transaction

from core import audit
from core.deadlines import db_now
from core.models import AuditAction

from .models import SigningKey

LOCK = "dogfood:signing-key"


class SigningUnavailable(Exception):
    status = 503
    code = "signing_unavailable"


def _ed25519():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    return Ed25519PrivateKey, Ed25519PublicKey


def raw_public(private_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def kid_of(raw_public_key: bytes) -> str:
    return hashlib.sha256(raw_public_key).hexdigest()[:16]


def key_dir():
    return str(settings.SIGNING_KEY_DIR)


def pem_path(kid):
    return os.path.join(key_dir(), f"{kid}.pem")


def _write_pem(private_key):
    """Write a new private key file (O_EXCL, 0600, in a 0700 directory); return (kid, raw public)."""
    from cryptography.hazmat.primitives import serialization

    raw = raw_public(private_key)
    kid = kid_of(raw)
    folder = key_dir()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except PermissionError:
        pass  # a directory someone else owns (a bind mount): its own permissions stand
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    fd = os.open(pem_path(kid), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return kid, raw


def _new_key():
    private, _ = _ed25519()
    return private.generate()


def load_private(kid):
    """The private key of `kid` from its PEM, or None when the file is missing, unreadable, or not the
    key the row says (its public half must derive the same kid)."""
    from cryptography.hazmat.primitives import serialization

    try:
        with open(pem_path(kid), "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    private, _ = _ed25519()
    if not isinstance(key, private) or kid_of(raw_public(key)) != kid:
        return None
    return key


def active():
    return SigningKey.objects.filter(retired_at__isnull=True).first()


def _lock():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [LOCK])


def ensure_signing_key():
    """Boot: ("created" | "ok" | "missing" | "mismatch", kid). Never raises for a missing file."""
    with transaction.atomic():
        _lock()
        row = active()
        if row is not None:
            if load_private(row.kid) is not None:
                return "ok", row.kid
            state = "missing" if not os.path.exists(pem_path(row.kid)) else "mismatch"
            return state, row.kid
        key = _new_key()
        kid, raw = _write_pem(key)
        try:
            SigningKey.objects.create(kid=kid, public_key=base64.b64encode(raw).decode("ascii"),
                                      created_at=db_now())
        except BaseException:
            os.unlink(pem_path(kid))
            raise
    audit.record(AuditAction.SIGNING_KEY_CREATED, subject=kid, kid=kid)
    return "created", kid


def rotate(*, actor=None, origin=None):
    """A new active key; the old one retired (and still published). Returns (old kid or None, new kid)."""
    key = _new_key()
    kid, raw = _write_pem(key)
    old = None
    try:
        with transaction.atomic():
            _lock()
            current = SigningKey.objects.select_for_update().filter(retired_at__isnull=True).first()
            now = db_now()
            if current is not None:
                old = current.kid
                SigningKey.objects.filter(pk=current.pk).update(retired_at=now)  # retire first
            SigningKey.objects.create(kid=kid, public_key=base64.b64encode(raw).decode("ascii"), created_at=now)
    except BaseException:
        os.unlink(pem_path(kid))
        raise
    audit.record(AuditAction.SIGNING_KEY_ROTATED, origin=origin, actor=actor, subject=kid, old=old, new=kid)
    return old, kid


def sign(message: bytes):
    """(kid, base64 signature) with the active key, or SigningUnavailable."""
    row = active()
    key = load_private(row.kid) if row is not None else None
    if key is None:
        raise SigningUnavailable("This install cannot sign right now: its signing key file is missing. "
                                 "An operator must run `manage.py rotate_signing_key`.")
    return row.kid, base64.b64encode(key.sign(message)).decode("ascii")


def verify(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature

    _, public = _ed25519()
    try:
        key = public.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), message)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
