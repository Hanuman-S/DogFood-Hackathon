"""Every write the accounts module makes goes through a function in this file.

Views stay thin: they parse input, call one of these, and render. That way the HTML pages and
the API (added later) cannot enforce different rules for the same action.
"""

from dataclasses import dataclass

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone

from accounts.models import ApiToken, User, UserSession, normalize_email
from accounts.throttle import is_throttled
from core import audit
from core.models import AuditAction
from core.net import client_ip, user_agent


@dataclass
class LoginResult:
    user: User | None = None
    error: str = ""  # "", "invalid", "throttled"


def attempt_login(request, email, password, remember):
    """Check the throttle, then the password, then start a session.

    The throttle is checked *before* the password, so a throttled caller learns nothing about
    whether their guess was right. Wrong email and wrong password produce the same error, and
    Django's ModelBackend hashes a dummy password for unknown emails so the two take the same
    time.
    """
    email = normalize_email(email)
    ip = client_ip(request)

    if is_throttled(email, ip):
        audit.record(AuditAction.LOGIN_THROTTLED, request=request, subject=email)
        return LoginResult(error="throttled")

    user = authenticate(request, username=email, password=password)
    if user is None:
        audit.record(AuditAction.LOGIN_FAILED, request=request, subject=email)
        return LoginResult(error="invalid")

    # login() rotates the session key (no session fixation) and fires user_logged_in, which
    # records the UserSession row and the audit entry -- see accounts/signals.py.
    login(request, user)
    if not remember:
        # Cookie dies with the browser. The server-side row still expires after
        # SESSION_COOKIE_AGE, so an abandoned session cannot live forever.
        request.session.set_expiry(0)
    return LoginResult(user=user)


def logout_user(request):
    # logout() flushes the session and fires user_logged_out (signals.py cleans up).
    logout(request)


def register_participant(request, *, name, email, password):
    """Public sign-up. Always creates a participant: nobody can sign themselves up as staff."""
    user = User.objects.create_user(email, password, name=name)
    audit.record(AuditAction.SIGNUP, request=request, actor=user, subject=user.email)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return user


def create_account(request, *, name, email, password, can_create_events=False, is_platform_admin=False):
    """An admin creating an account for someone else (an organizer or judge, typically).

    Judge and organizer roles are then granted per event, from the event's control page."""
    user = User.objects.create_user(
        email, password, name=name,
        can_create_events=can_create_events or is_platform_admin, is_platform_admin=is_platform_admin,
    )
    audit.record(
        AuditAction.ACCOUNT_CREATED, request=request, subject=user.email,
        can_create_events=user.can_create_events, is_platform_admin=user.is_platform_admin,
    )
    return user


def start_session(request, user):
    """Called on every login (signals.py): remember which browser this session belongs to."""
    now = timezone.now()
    UserSession.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={
            "user": user,
            "ip": client_ip(request),
            "user_agent": user_agent(request),
            "created_at": now,
            "last_seen_at": now,
        },
    )


def end_session(session_key):
    """End one session everywhere: Django's session row and our metadata row."""
    with transaction.atomic():
        Session.objects.filter(session_key=session_key).delete()
        UserSession.objects.filter(session_key=session_key).delete()


def live_sessions(user):
    """The user's sessions that Django still considers valid, newest activity first.

    Expired sessions leave UserSession rows behind until `clearsessions` runs; joining on the
    live django_session rows filters them out, and removes the orphans while we are here.
    """
    rows = list(UserSession.objects.filter(user=user))
    live_keys = set(
        Session.objects.filter(
            session_key__in=[r.session_key for r in rows], expire_date__gt=timezone.now()
        ).values_list("session_key", flat=True)
    )
    dead = [r.pk for r in rows if r.session_key not in live_keys]
    if dead:
        UserSession.objects.filter(pk__in=dead).delete()
    return [r for r in rows if r.session_key in live_keys]


def revoke_session(request, user_session):
    is_current = user_session.session_key == request.session.session_key
    audit.record(
        AuditAction.SESSION_REVOKED, request=request, subject=user_session.device,
        ip_of_session=user_session.ip, current=is_current,
    )
    if is_current:
        logout_user(request)
    else:
        end_session(user_session.session_key)
    return is_current


def revoke_other_sessions(request, reason=AuditAction.SESSIONS_REVOKED_OTHERS):
    current = request.session.session_key
    others = UserSession.objects.filter(user=request.user).exclude(session_key=current)
    keys = list(others.values_list("session_key", flat=True))
    for key in keys:
        end_session(key)
    if reason:
        audit.record(reason, request=request, count=len(keys))
    return len(keys)


def change_password(request, form):
    """Save a new password, keep this session, end every other one.

    Django already invalidates other sessions when the password hash changes (each session
    stores a hash of the password it was created with). We also delete them outright, so the
    'active sessions' list is truthful immediately rather than on each stale session's next
    request.
    """
    old_key = request.session.session_key
    user = form.save()
    update_session_auth_hash(request, user)  # rotates this session's key
    UserSession.objects.filter(session_key=old_key).update(
        session_key=request.session.session_key
    )
    ended = revoke_other_sessions(request, reason=None)
    audit.record(AuditAction.PASSWORD_CHANGED, request=request, other_sessions_ended=ended)
    return user


def issue_token(request, name):
    token, raw = ApiToken.issue(request.user, name)
    audit.record(AuditAction.TOKEN_CREATED, request=request, subject=token.prefix, name=name)
    return token, raw


def revoke_token(request, token):
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        audit.record(AuditAction.TOKEN_REVOKED, request=request, subject=token.prefix)
