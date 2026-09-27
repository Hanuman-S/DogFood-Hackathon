"""Login, logout, sign-up and the account page (sessions, tokens, password).

Views only parse input and render; every rule lives in accounts/services.py.
"""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts import services
from accounts.forms import LoginForm, PasswordChangeForm, SignupForm, TokenForm
from accounts.guards import login_required
from accounts.models import ApiToken, UserSession
from accounts.roles import PORTAL_URL, home_portal as home_portal_name
from core.views import forbidden

LOGIN_ERRORS = {
    "invalid": "invalid email or password.",
    "throttled": "too many failed attempts. wait 15 minutes, then try again.",
}


def home_portal(user):
    return reverse(PORTAL_URL[home_portal_name(user)])


def _safe_next(request):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return ""


@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request) or home_portal(request.user))

    form = LoginForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        result = services.attempt_login(
            request,
            form.cleaned_data["email"],
            form.cleaned_data["password"],
            form.cleaned_data["remember"],
        )
        if result.user:
            return redirect(_safe_next(request) or home_portal(result.user))
        form.add_error(None, LOGIN_ERRORS[result.error])
        status = 429 if result.error == "throttled" else 200
    elif request.method == "POST":
        status = 400

    return render(
        request, "accounts/login.html",
        {"form": form, "next": _safe_next(request), "signup_open": settings.ALLOW_SIGNUP},
        status=status,
    )


@require_POST
def logout_view(request):
    """POST only: a GET logout can be triggered by any <img> tag on any website."""
    services.logout_user(request)
    messages.success(request, "session closed. goodbye.")
    return redirect("public:home")


@never_cache
def signup_view(request):
    if not settings.ALLOW_SIGNUP:
        return forbidden(request, reason="public sign-up is closed for this portal.")
    if request.user.is_authenticated:
        return redirect(home_portal(request.user))

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = services.register_participant(
            request,
            name=form.cleaned_data["name"],
            email=form.cleaned_data["email"],
            password=form.cleaned_data["password1"],
        )
        messages.success(request, f"account created. welcome, {user.get_short_name()}.")
        return redirect(_safe_next(request) or home_portal(user))
    return render(
        request, "accounts/signup.html", {"form": form, "next": _safe_next(request)},
        status=400 if request.method == "POST" else 200,
    )


@never_cache
@login_required
def account_view(request, password_form=None, token_form=None, status=200):
    return render(
        request,
        "accounts/account.html",
        {
            "sessions": services.live_sessions(request.user),
            "current_session_key": request.session.session_key,
            "tokens": ApiToken.objects.filter(user=request.user),
            # A freshly issued token is shown exactly once, then forgotten.
            "new_token": request.session.pop("new_token", None),
            "password_form": password_form or PasswordChangeForm(request.user),
            "token_form": token_form or TokenForm(),
        },
        status=status,
    )


@require_POST
@login_required
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST)
    if not form.is_valid():
        return account_view(request, password_form=form, status=400)
    services.change_password(request, form)
    messages.success(request, "password changed. every other session has been signed out.")
    return redirect("accounts:account")


@require_POST
@login_required
def session_revoke(request, session_id):
    # Scoped to request.user: another user's session id is a 404, not a revoke.
    user_session = get_object_or_404(UserSession, pk=session_id, user=request.user)
    if services.revoke_session(request, user_session):
        messages.success(request, "this session was closed.")
        return redirect("public:home")
    messages.success(request, "session revoked.")
    return redirect("accounts:account")


@require_POST
@login_required
def sessions_revoke_others(request):
    count = services.revoke_other_sessions(request)
    messages.success(request, f"{count} other session{'s' if count != 1 else ''} signed out.")
    return redirect("accounts:account")


@require_POST
@login_required
def token_create(request):
    form = TokenForm(request.POST)
    if not form.is_valid():
        return account_view(request, token_form=form, status=400)
    token, raw = services.issue_token(request, form.cleaned_data["name"])
    request.session["new_token"] = {"name": token.name, "raw": raw}
    return redirect(reverse("accounts:account") + "#tokens")


@require_POST
@login_required
def token_revoke(request, token_id):
    token = get_object_or_404(ApiToken, pk=token_id, user=request.user)
    services.revoke_token(request, token)
    messages.success(request, f"token {token.prefix}… revoked.")
    return redirect(reverse("accounts:account") + "#tokens")
