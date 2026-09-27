"""Field validators for project content.

Kept separate from `models.py` because migrations serialize a reference to the validator
function, so its import path becomes part of the migration history and should be stable.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

# Only these two schemes are accepted anywhere a user supplies a URL.
#
# Django's URLField already rejects most nonsense, but its default scheme list includes ftp and
# ftps, and a bare `URLValidator` will happily accept `javascript:`-style input in some Django
# versions when combined with permissive form handling. Since these URLs are rendered as links
# in a page other people visit, the safe set is stated explicitly rather than inherited.
ALLOWED_URL_SCHEMES = ("http", "https")


def validate_web_url(value: str) -> None:
    """Accept only absolute http(s) URLs with a host.

    Rejecting `javascript:`, `data:` and `file:` here matters because these values are rendered
    into `href` attributes. Template autoescaping protects the attribute *syntax*; it does not
    stop a `javascript:` URL from being a working link.
    """
    if not value:
        return

    parts = urlsplit(value.strip())

    if parts.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValidationError(
            "Enter a URL starting with http:// or https:// "
            f"(got {parts.scheme or 'no scheme'}).",
            code="invalid_scheme",
        )

    if not parts.netloc:
        raise ValidationError("Enter a full URL including a domain name.", code="no_host")
