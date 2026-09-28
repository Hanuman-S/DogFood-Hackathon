#!/usr/bin/env python3
"""Verify a DOGFOOD signed record OFFLINE, with nothing but Python 3 (no packages, no network).

    python3 scripts/verify_record.py record.json keys.json [--revoked revoked.json]

record.json   the record's download (/records/<id>.json): payload_text, signature, kid
keys.json     the portal's /.well-known/dogfood-signing-keys.json (or one key: {"kid", "public_key"})
revoked.json  optional: the portal's /.well-known/dogfood-revoked.json (sha256 of each revoked id)

Exit status: 0 valid; 1 invalid, unknown key or unreadable input; 2 validly signed but revoked.

What a valid signature proves: the holder of that key signed exactly these bytes. It does not prove
the record still stands -- a record can be revoked after it was signed. Check the revoked list (from
the portal, as fresh as you can get it) for that.

The Ed25519 verification below follows the reference implementation in RFC 8032, section 6
(Josefsson and Liusvaara, "Edwards-Curve Digital Signature Algorithm (EdDSA)", 2017), which the IETF
publishes under the Simplified BSD License. The standard library has no Ed25519, so it is vendored
here; it is slow and not constant-time, which is fine for checking a public signature.
"""

import base64
import hashlib
import json
import sys

# --- Ed25519 (RFC 8032, section 6) --------------------------------------------------------------------

p = 2 ** 255 - 19
q = 2 ** 252 + 27742317777372353535851937790883648493  # the group order (L)


def _sha512_modq(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % q


def _modp_inv(x):
    return pow(x, p - 2, p)


d = -121665 * _modp_inv(121666) % p
modp_sqrt_m1 = pow(2, (p - 1) // 4, p)


def _point_add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % p, (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C, D = 2 * P[3] * Q[3] * d % p, 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _point_mul(s, P):
    Q = (0, 1, 1, 0)  # the neutral element
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % p != 0:
        return False
    return True


def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * _modp_inv(d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * modp_sqrt_m1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


g_y = 4 * _modp_inv(5) % p
g_x = _recover_x(g_y, 0)
G = (g_x, g_y, 1, g_x * g_y % p)


def _point_decompress(s):
    if len(s) != 32:
        raise ValueError("an encoded point is 32 bytes")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def ed25519_verify(public, message, signature):
    """True if `signature` (64 bytes) is `public`'s (32 bytes) Ed25519 signature of `message`."""
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _point_decompress(public)
    if not A:
        return False
    Rs = signature[:32]
    R = _point_decompress(Rs)
    if not R:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= q:
        return False
    h = _sha512_modq(Rs + public + message)
    sB = _point_mul(s, G)
    hA = _point_mul(h, A)
    return _point_equal(sB, _point_add(R, hA))


# --- the record ---------------------------------------------------------------------------------------------

def _keys(data):
    if isinstance(data, dict) and "keys" in data:
        return {k["kid"]: k["public_key"] for k in data["keys"]}
    if isinstance(data, dict) and "kid" in data:
        return {data["kid"]: data["public_key"]}
    raise ValueError("keys.json is neither the portal's key list nor a single key")


def check(record, keys, revoked=None):
    """(exit status, message)."""
    text = record["payload_text"].encode("utf-8")
    try:
        payload = json.loads(text)
    except ValueError:
        return 1, "invalid: payload_text is not JSON"
    kid = payload.get("kid", record.get("kid"))
    if kid != record.get("kid"):
        return 1, "invalid: the payload names another key than the record does"
    if kid not in keys:
        return 1, f"unknown key: {kid} is not in the key list"
    try:
        public = base64.b64decode(keys[kid], validate=True)
        signature = base64.b64decode(record["signature"], validate=True)
    except ValueError:
        return 1, "invalid: the key or the signature is not base64"
    if not ed25519_verify(public, text, signature):
        return 1, "invalid: the signature does not match these exact bytes"
    if revoked is not None:
        digest = hashlib.sha256(str(payload.get("record_id", "")).lower().encode("utf-8")).hexdigest()
        if digest in {r.get("record_id_sha256") for r in revoked.get("revoked", [])}:
            return 2, f"revoked: validly signed by {kid}, but the portal lists it as revoked"
    return 0, f"valid: signed by {kid}" + ("" if revoked is not None else " (revocation not checked)")


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    revoked_path = None
    if "--revoked" in argv:
        i = argv.index("--revoked")
        if i + 1 >= len(argv):
            print("usage: verify_record.py record.json keys.json [--revoked revoked.json]", file=sys.stderr)
            return 1
        revoked_path = argv[i + 1]
        args = [a for a in args if a != revoked_path]
    if len(args) != 2:
        print("usage: verify_record.py record.json keys.json [--revoked revoked.json]", file=sys.stderr)
        return 1
    try:
        with open(args[0], encoding="utf-8") as fh:
            record = json.load(fh)
        with open(args[1], encoding="utf-8") as fh:
            keys = _keys(json.load(fh))
        revoked = None
        if revoked_path:
            with open(revoked_path, encoding="utf-8") as fh:
                revoked = json.load(fh)
        status, message = check(record, keys, revoked)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"unreadable input: {error}", file=sys.stderr)
        return 1
    print(message)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
