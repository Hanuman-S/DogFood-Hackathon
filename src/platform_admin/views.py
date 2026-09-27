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


@never_cache
@portal_required("admin")
def home(request, form=None, status=200):
    counts = dict(User.objects.values_list("role").annotate(n=Count("id")))
    return render(
        request,
        "platform_admin/home.html",
        {
            "role_counts": [(label, counts.get(value, 0)) for value, label in Role.choices],
            "users": User.objects.order_by("role", "email")[:50],
            "audit": AuditLog.objects.select_related("actor")[:25],
            "form": form or AccountCreateForm(),
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
        role=form.cleaned_data["role"],
        password=form.cleaned_data["password1"],
    )
    messages.success(request, f"{user.get_role_display().lower()} account created for {user.email}.")
    return redirect("platform_admin:home")
