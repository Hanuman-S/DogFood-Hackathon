"""Hooks on Django's own login/logout signals, so *every* login path is tracked -- ours, the
database admin's, and any added later -- without each one remembering to."""

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from accounts.models import UserSession
from accounts.services import start_session
from core import audit
from core.models import AuditAction


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    if request is None or not hasattr(request, "session"):
        return
    start_session(request, user)
    audit.record(AuditAction.LOGIN_OK, request=request, actor=user, subject=user.email)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if request is None:
        return
    key = request.session.session_key
    if key:
        UserSession.objects.filter(session_key=key).delete()
    if user is not None:
        audit.record(AuditAction.LOGOUT, request=request, actor=user, subject=user.email)
