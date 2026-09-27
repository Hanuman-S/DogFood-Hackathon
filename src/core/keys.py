"""Keys derived from SECRET_KEY, one per purpose, so no two uses share a key:

    derived_key(purpose) = HMAC-SHA256(SECRET_KEY, purpose)

Purposes in use: "ip-hash" (core.net.hash_ip), "voter-links" and "open-link" (voting.links).
Django's own signing (sessions, the open-link cookie) keeps its own salted derivation.
"""

import hashlib
import hmac

from django.conf import settings


def derived_key(purpose: str) -> bytes:
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


def keyed_hash(purpose: str, message: str) -> str:
    """HMAC-SHA256(derived_key(purpose), message), as 64 hex characters."""
    return hmac.new(derived_key(purpose), message.encode("utf-8"), hashlib.sha256).hexdigest()
