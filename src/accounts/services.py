"""Account operations: signup, login, password change, API tokens.

Every write goes through a function here rather than happening in a view, so the browser and the
API produce identical audit trails for identical actions.

The login throttle lives here too. It counts the `login.failed` audit rows the brief already
requires us to write, rather than keeping a second counter somewhere:

* One store, so the count and the audit trail can never disagree.
* It survives a restart, unlike an in-process cache.
* It applies across gunicorn workers, unlike per-process memory. The image runs two workers, so a
  per-process counter would let an attacker get 2x the allowance by luck of load balancing.

The cost is a query per login attempt, which is nothing next to hashing a password.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import authenticate, login as django_login, logout as django_logout
from django.db import transaction

from accounts.models import ApiToken, User
from core import audit, clock
from core.errors import ConflictError, PortalError, ValidationFailed
from core.models import AuditAction, AuditLog


class LoginThrottled(PortalError):
    """Too many recent failures for this email and IP."""

    code = "login_throttled"
    status_code = 429
    message = "Too many failed sign-in attempts. Try again shortly."


class InvalidCredentials(PortalError):
    code = "invalid_credentials"
    status_code = 400
    message = "That email and password do not match an account."


# --------------------------------------------------------------------------------------
# signup
# --------------------------------------------------------------------------------------


@transaction.atomic
def register_account(*, email: str, display_name: str, password: str, request=None) -> User:
    """Create a portal account.

    Password strength is Django's `AUTH_PASSWORD_VALIDATORS`, applied by the form before this is
    called. Email uniqueness is a database constraint; the race between two simultaneous signups
    for the same address is therefore decided by Postgres, not by a check here.
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValidationFailed("An email address is required.")

    if User.objects.filter(email=email).exists():
        # Deliberately explicit rather than vague. This address is already visible to whoever
        # holds it, and pretending otherwise would just produce a confusing dead end at signup.
        # The password reset flow is where address-existence leaks actually matter, and this
        # portal has none (see the README).
        raise ConflictError("An account with that email address already exists.")

    user = User.objects.create_user(
        email=email,
        password=password,
        display_name=(display_name or "").strip() or email.split("@")[0],
    )

    audit.record(AuditAction.SIGNUP, actor=user, target=user, request=request)
    return user


# --------------------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ThrottleState:
    """How close this (email, IP) pair is to being locked out."""

    failures: int
    limit: int
    window_minutes: int

    @property
    def is_throttled(self) -> bool:
        return self.failures >= self.limit

    @property
    def remaining(self) -> int:
        return max(self.limit - self.failures, 0)


def throttle_state(*, email: str, ip: str) -> ThrottleState:
    """Count recent failed attempts for this email and IP.

    Scoped to the pair, not to either alone. Counting by email only would let anyone lock a known
    user out of their own account by failing five times on their behalf; counting by IP only would
    lock out everyone behind a shared NAT as soon as one person fumbled a password.
    """
    limit = getattr(settings, "LOGIN_FAILURE_LIMIT", 5)
    window = getattr(settings, "LOGIN_FAILURE_WINDOW_MINUTES", 15)
    since = clock.now() - dt.timedelta(minutes=window)

    failures = AuditLog.objects.filter(
        action=AuditAction.LOGIN_FAILED,
        created_at__gte=since,
        metadata__email=(email or "").strip().lower(),
        metadata__ip=ip or "",
    ).count()

    return ThrottleState(failures=failures, limit=limit, window_minutes=window)


def attempt_login(*, request, email: str, password: str) -> User:
    """Authenticate and open a session, or raise.

    Order matters: the throttle is checked **before** the password is verified. Checking it
    afterwards would still do the expensive hash comparison on every attempt, which is most of what
    a throttle is supposed to prevent.

    Raises:
        LoginThrottled: too many recent failures for this email and IP.
        InvalidCredentials: no match. Deliberately the same error whether the account does not
            exist, has an unusable password, or the password is simply wrong.
    """
    email = (email or "").strip().lower()
    ip = audit.client_ip(request)

    state = throttle_state(email=email, ip=ip)
    if state.is_throttled:
        audit.record(
            AuditAction.LOGIN_THROTTLED,
            metadata={
                "email": email,
                "ip": ip,
                "failures": state.failures,
                "window_minutes": state.window_minutes,
            },
            request=request,
        )
        raise LoginThrottled(
            "Too many failed sign-in attempts for this account from this address. "
            f"Wait {state.window_minutes} minutes and try again."
        )

    user = authenticate(request, username=email, password=password)

    if user is None:
        # `email` and `ip` go in the metadata because that is what `throttle_state` counts on.
        # Changing either key name breaks the throttle silently, so a test asserts the round trip.
        audit.record(
            AuditAction.LOGIN_FAILED,
            metadata={"email": email, "ip": ip},
            request=request,
        )
        raise InvalidCredentials()

    django_login(request, user)
    audit.record(AuditAction.LOGIN_SUCCEEDED, actor=user, target=user, request=request)
    return user


def log_out(request) -> None:
    """End the session. Called only from a POST -- see the note in `accounts/views.py`."""
    user = request.user if is_logged_in(request) else None
    django_logout(request)
    if user is not None:
        audit.record(AuditAction.LOGOUT, actor=user, target=user, request=request)


def is_logged_in(request) -> bool:
    return bool(getattr(request, "user", None) and request.user.is_authenticated)


def record_password_change(*, user, request=None) -> None:
    """Audit a password change. The change itself is Django's form; this records that it happened.

    Worth recording because a password change is the step an account takeover ends with, so it is
    one of the few events an organizer reviewing the trail genuinely needs to see.
    """
    audit.record(AuditAction.PASSWORD_CHANGED, actor=user, target=user, request=request)


# --------------------------------------------------------------------------------------
# API tokens
# --------------------------------------------------------------------------------------

MAX_TOKENS_PER_USER = 20


@transaction.atomic
def create_token(*, user: User, name: str, request=None) -> tuple[ApiToken, str]:
    """Issue a token, returning `(token, plaintext)`.

    The plaintext is returned once, for display, and is not recoverable afterwards -- only its
    SHA-256 digest is stored. The caller is responsible for showing it exactly once and never
    putting it in a log line or an audit entry.
    """
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Give the token a name, so you can recognise it later.")

    active = ApiToken.objects.filter(user=user, revoked_at__isnull=True).count()
    if active >= MAX_TOKENS_PER_USER:
        raise ConflictError(
            f"You already have {MAX_TOKENS_PER_USER} active tokens. Revoke one first."
        )

    token, plaintext = ApiToken.issue(user=user, name=name)

    # Records that a token was created, never its value or digest.
    audit.record(
        AuditAction.TOKEN_CREATED,
        actor=user,
        target=token,
        metadata={"name": name},
        request=request,
    )
    return token, plaintext


@transaction.atomic
def revoke_token(*, user: User, token_id: int, request=None) -> ApiToken:
    """Revoke one of the caller's own tokens.

    Scoped to `user=user` in the lookup itself, so another account's token id simply does not
    resolve -- a 404, not a 403, because whether that id exists is none of the caller's business.
    """
    token = ApiToken.objects.filter(pk=token_id, user=user).first()
    if token is None:
        raise ValidationFailed("No such token.")

    token.revoke()
    audit.record(
        AuditAction.TOKEN_REVOKED,
        actor=user,
        target=token,
        metadata={"name": token.name},
        request=request,
    )
    return token
