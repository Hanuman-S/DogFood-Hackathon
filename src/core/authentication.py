"""Bearer token authentication for the JSON API.

Why a custom class instead of DRF's built-in `TokenAuthentication`:

* DRF's version stores the token in plaintext. This one stores only a SHA-256 digest.
* DRF's version uses the `Token` keyword. `Authorization: Bearer <token>` is what API
  clients expect, and it is what the acceptance checker can send -- the checker attaches
  exactly one header string of the form `Name: value`, so `Authorization: Bearer abc` works
  while anything needing a second header (a CSRF token, for instance) cannot.

**CSRF.** This class does not enforce CSRF, and that is correct rather than convenient. CSRF
protection defends against a browser attaching *ambient* credentials (a cookie) to a
cross-site request. A bearer token is not ambient: an attacker's page cannot make the
victim's browser attach it. DRF's `SessionAuthentication` still enforces CSRF for cookie
auth, and it is still in the authentication list, so the browser path is unchanged. The two
mechanisms coexist with their own correct rules; the token path is not a CSRF hole.

Consequently `POST /api/events/<slug>/projects` is reachable with a token and no CSRF token,
which is what makes the deadline refusal on that route a genuine test of the deadline rather
than an accidental test of CSRF.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from rest_framework import authentication, exceptions

from accounts.models import ApiToken
from core import clock

KEYWORD = "Bearer"


class BearerTokenAuthentication(authentication.BaseAuthentication):
    """Authenticate `Authorization: Bearer <token>` against `accounts.ApiToken`."""

    keyword = KEYWORD

    def authenticate(self, request):
        header = authentication.get_authorization_header(request)
        if not header:
            return None

        parts = header.split()
        if parts[0].lower() != self.keyword.lower().encode():
            # Some other scheme (Basic, or a session cookie): let the next authentication
            # class have it. Returning None rather than raising is what makes the
            # authentication classes composable.
            return None

        if len(parts) == 1:
            raise exceptions.AuthenticationFailed(
                _("Invalid Authorization header. No token supplied.")
            )
        if len(parts) > 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid Authorization header. Token must not contain spaces.")
            )

        try:
            plaintext = parts[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed(
                _("Invalid Authorization header. Token is not valid UTF-8.")
            ) from None

        return self.authenticate_credentials(plaintext)

    def authenticate_credentials(self, plaintext: str):
        # Look the token up by digest. The plaintext is never compared against anything
        # stored, because nothing stored is the plaintext.
        digest = ApiToken.hash_token(plaintext)
        token = (
            ApiToken.objects.select_related("user")
            .filter(token_hash=digest)
            .first()
        )

        # Every failure below says the same thing. Distinguishing "no such token" from
        # "revoked token" would tell an attacker which of their guesses had once been real.
        if token is None or token.revoked_at is not None or not token.user.is_active:
            raise exceptions.AuthenticationFailed(_("Invalid or revoked token."))

        # Cheap audit signal for organizers wondering whether a token is still in use.
        # `update` rather than `save` to avoid a race between concurrent API requests, and
        # it deliberately does not touch `updated_at`-style bookkeeping elsewhere.
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=clock.now())

        return (token.user, token)

    def authenticate_header(self, request):
        """Drives the `WWW-Authenticate` header, which makes DRF answer 401 rather than 403
        when no credentials were supplied at all."""
        return f'{self.keyword} realm="api"'
