"""Auth and profile views. Thin: validate the form, call a service, render the result."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from accounts import services
from accounts.forms import LoginForm, PasswordChangeForm, SignupForm, TokenCreateForm
from accounts.models import ApiToken
from accounts.services import InvalidCredentials, LoginThrottled
from core.errors import PortalError

# Where to send someone after login. Only relative paths are honoured -- see `safe_next`.
DEFAULT_REDIRECT = "/"


def safe_next(request) -> str:
    """Read `?next=` without becoming an open redirect.

    An absolute URL here would let a phishing page send someone to a real login form and bounce
    them to a lookalike afterwards, with the portal's own domain in the referring link. Only
    same-site absolute *paths* are accepted.
    """
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return DEFAULT_REDIRECT


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect(DEFAULT_REDIRECT)

    form = SignupForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            user = services.register_account(
                email=form.cleaned_data["email"],
                display_name=form.cleaned_data["display_name"],
                password=form.cleaned_data["password1"],
                request=request,
            )
        except PortalError as error:
            form.add_error("email" if error.code == "conflict" else None, error.message)
        else:
            # Signing up logs you in. The invite flow depends on it: a visitor who opens an invite
            # link, signs up and is then dumped at a login form would have to find the link again.
            services.attempt_login(
                request=request,
                email=user.email,
                password=form.cleaned_data["password1"],
            )
            messages.success(request, f"Welcome, {user.display_name}.")
            return redirect(safe_next(request))

    return render(request, "accounts/signup.html", {"form": form, "next": safe_next(request)})


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect(safe_next(request))

    form = LoginForm(request.POST or None)
    status = 200

    if request.method == "POST" and form.is_valid():
        try:
            services.attempt_login(
                request=request,
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
            )
        except LoginThrottled as error:
            # 429 rather than 200-with-a-message, so an API client or a monitoring check sees the
            # throttle for what it is.
            form.add_error(None, error.message)
            status = error.status_code
        except InvalidCredentials as error:
            form.add_error(None, error.message)
            status = 401
        else:
            return redirect(safe_next(request))

    return render(
        request,
        "accounts/login.html",
        {"form": form, "next": safe_next(request)},
        status=status,
    )


@require_POST
def logout_view(request):
    """POST only.

    A GET logout can be triggered by any third-party page embedding
    `<img src="http://portal/logout">`, which is a real if minor annoyance, and it breaks browser
    prefetching in unpleasant ways. The nav bar posts a small form.
    """
    services.log_out(request)
    messages.success(request, "Signed out.")
    return redirect(DEFAULT_REDIRECT)


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    """Profile page: account details and API tokens.

    A created token's plaintext is passed to the template through a one-shot `messages` entry
    rather than being stored anywhere, so a page refresh cannot redisplay it.
    """
    token_form = TokenCreateForm(request.POST or None)
    created_plaintext = None

    if request.method == "POST" and token_form.is_valid():
        try:
            _, created_plaintext = services.create_token(
                user=request.user,
                name=token_form.cleaned_data["name"],
                request=request,
            )
        except PortalError as error:
            token_form.add_error(None, error.message)
        else:
            token_form = TokenCreateForm()

    tokens = ApiToken.objects.filter(user=request.user).order_by("revoked_at", "-created_at")

    # Shows the caller which roles they hold and where. Reads from the database like every other
    # page -- there is no hardcoded role list in the template.
    memberships = (
        request.user.event_memberships.select_related("event")
        .order_by("event__name", "role")
    )

    return render(
        request,
        "accounts/profile.html",
        {
            "token_form": token_form,
            "tokens": tokens,
            "memberships": memberships,
            # Shown once, then gone. Not stored in the session: a session-stored secret would
            # survive a refresh and end up in a database backup.
            "created_plaintext": created_plaintext,
        },
    )


@login_required
@require_POST
def revoke_token(request, token_id: int):
    try:
        token = services.revoke_token(user=request.user, token_id=token_id, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, f"Revoked the token “{token.name}”.")
    return redirect(reverse("profile"))


@login_required
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = PasswordChangeForm(user=request.user, data=request.POST or None)

    if request.method == "POST" and form.is_valid():
        form.save()
        # Django rotates the session hash on a password change, which would otherwise sign the
        # user out of the tab they just used to change it.
        update_session_auth_hash(request, form.user)
        services.record_password_change(user=request.user, request=request)
        messages.success(request, "Password changed.")
        return redirect(reverse("profile"))

    return render(request, "accounts/password_change.html", {"form": form})
