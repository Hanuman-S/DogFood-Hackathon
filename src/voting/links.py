"""Tokens for the two link-based access modes, and the open-link voter cookie.

* email_gated: each VoterLink's token = HMAC(derived_key("voter-links"), "<link id>:<nonce>"), with
  derived_key(p) = HMAC(SECRET_KEY, p) (core.keys). It is
  derived, not stored (the table holds only its SHA-256 digest), so the organizer can download
  voter-links.csv again at any time. Rotating SECRET_KEY changes every token.
* open_link: the event's one token = HMAC(derived_key("open-link"), "<event id>:<open_link_nonce>"). A new nonce is a new
  link; the old one stops resolving.
* The open-link voter is a random id in a cookie signed with Django's signing (salt
  "voting.open_link"), scoped to the event. It stops casual double voting in one browser and no more:
  a new browser or cleared cookies is a new voter. That is why open_link is the weakest mode.

Both token kinds are 64 hex characters, looked up by digest, compared in constant time.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.crypto import constant_time_compare

from core.keys import keyed_hash

COOKIE_SALT = "voting.open_link"
MAX_EMAILS = 5000


def new_nonce():
    return secrets.token_hex(16)


def link_token(link) -> str:
    return keyed_hash("voter-links", f"{link.pk}:{link.nonce}")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def open_token(config) -> str:
    return keyed_hash("open-link", f"{config.event_id}:{config.open_link_nonce}")


def is_open_token(config, token) -> bool:
    return bool(config.open_link_nonce) and constant_time_compare(open_token(config), token or "")


# --- the open-link cookie ------------------------------------------------------------------------

VOTER_ID = re.compile(r"[0-9a-f]{32}")


def cookie_name(event):
    return f"dogfood_vote_{event.pk}"


def new_cookie_value(event):
    return signing.dumps({"e": event.pk, "v": secrets.token_hex(16)}, salt=COOKIE_SALT)


def cookie_voter_id(event, value) -> str:
    """The voter id in a signed cookie for this event, or "" (missing, tampered, or another event's)."""
    if not value:
        return ""
    try:
        data = signing.loads(value, salt=COOKIE_SALT)
    except signing.BadSignature:
        return ""
    if not isinstance(data, dict) or data.get("e") != event.pk or not isinstance(data.get("v"), str):
        return ""
    # Only the form new_cookie_value issues (32 hex). An imported ballot's voter is a pseudonym
    # ("v_" + 16 hex) stored in the same column; this makes sure no cookie -- even one signed with
    # this install's key -- can ever name it.
    if not VOTER_ID.fullmatch(data["v"]):
        return ""
    return data["v"]


# --- the allowlist ---------------------------------------------------------------------------------

def parse_emails(text="", csv_bytes=b""):
    """(valid emails, lower-cased and de-duplicated in first-seen order; rejected entries).

    Pasted text: separated by newlines, commas, semicolons or spaces. CSV: every cell that contains
    an "@" is taken as an email; other cells (names, headers) are ignored."""
    candidates = []
    for chunk in (text or "").replace(";", "\n").replace(",", "\n").split():
        candidates.append(chunk)
    if csv_bytes:
        try:
            decoded = csv_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("The file is not UTF-8 text.") from None
        for row in csv.reader(io.StringIO(decoded)):
            candidates.extend(cell.strip() for cell in row if "@" in cell)
    seen, valid, rejected = set(), [], []
    for raw in candidates:
        email = raw.strip().strip("<>\"'").lower()
        if not email:
            continue
        try:
            validate_email(email)
        except ValidationError:
            rejected.append(raw[:100])
            continue
        if email not in seen:
            seen.add(email)
            valid.append(email)
    if len(valid) > MAX_EMAILS:
        raise ValueError(f"At most {MAX_EMAILS} emails at a time.")
    return valid, rejected
