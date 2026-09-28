"""The admin portal: accounts across the whole platform, and the audit trail."""

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from accounts import services
from accounts.forms import AccountCreateForm
from accounts.guards import portal_required
from accounts.models import User
from accounts.roles import Role
from core.models import AuditLog
from events.models import EventMembership


def _signing():
    from records import keys
    state, kid = keys.status()
    return {"signing_state": state, "signing_kid": kid}


@never_cache
@portal_required("admin")
def home(request, form=None, status=200):
    by_role = dict(
        EventMembership.objects.values_list("role").annotate(n=Count("user", distinct=True))
    )
    counts = [
        ("accounts", User.objects.count()),
        ("platform admins", User.objects.filter(is_platform_admin=True).count()),
        ("may create events", User.objects.filter(can_create_events=True).count()),
    ] + [(f"{label.lower()}s (in any event)", by_role.get(value, 0)) for value, label in Role.choices]
    return render(
        request,
        "platform_admin/home.html",
        {
            "role_counts": counts,
            "users": User.objects.order_by("-is_platform_admin", "-can_create_events", "email")
            .prefetch_related("event_memberships__event")[:50],
            "audit": AuditLog.objects.select_related("actor")[:25],
            "form": form or AccountCreateForm(),
            **_signing(),
        },
        status=status,
    )


@portal_required("admin")
def account_create(request):
    if request.method != "POST":
        return redirect("platform_admin:home")
    form = AccountCreateForm(request.POST)
    if not form.is_valid():
        return home(request, form=form, status=400)
    user = services.create_account(
        request,
        name=form.cleaned_data["name"],
        email=form.cleaned_data["email"],
        password=form.cleaned_data["password1"],
        can_create_events=form.cleaned_data["can_create_events"],
        is_platform_admin=form.cleaned_data["is_platform_admin"],
    )
    messages.success(
        request,
        f"account created for {user.email}. make them a judge or organizer from an event's control page.",
    )
    return redirect("platform_admin:home")
