"""The raw database admin (Django's admin), mounted at /admin/db/ and open to admins only.

Its own login page is replaced by ours, so the login throttle, audit trail and session
tracking cannot be side-stepped through a second door.
"""

from functools import update_wrapper

from django.contrib.admin import AdminSite
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import reverse

from core.views import forbidden

REASON = "the database admin is open to admins only."


class DatabaseAdminSite(AdminSite):
    site_header = "NYANJARO // database"
    site_title = "NYANJARO database"
    index_title = "tables"

    def admin_view(self, view, cacheable=False):
        inner = super().admin_view(view, cacheable)

        def guarded(request, *args, **kwargs):
            if not self.has_permission(request):
                if request.user.is_authenticated:
                    # Logged in, wrong role. Sending them to /login would bounce straight
                    # back here and loop; say no instead.
                    return forbidden(request, reason=REASON)
                return redirect_to_login(request.get_full_path())
            return inner(request, *args, **kwargs)

        return update_wrapper(guarded, view)

    def login(self, request, extra_context=None):
        index = reverse(f"{self.name}:index")
        if self.has_permission(request):
            return redirect(index)
        if request.user.is_authenticated:
            return forbidden(request, reason=REASON)
        return redirect_to_login(index)


database_admin = DatabaseAdminSite(name="database_admin")
