"""The Django admin, gated on `is_platform_admin`.

The permission matrix says `/admin/` is admin-only: not organizers, not judges. Django's
default `AdminSite` admits anyone with `is_staff`, which is a weaker condition than the one
the portal actually means. Rather than rely on nobody ever setting `is_staff` by accident,
the check is narrowed here to the flag the rest of the portal treats as authoritative.

A non-admin gets the admin login screen rather than a 403, which is Django's own behaviour
for an unauthenticated visitor and leaks nothing about who does have access.
"""

from django.contrib.admin import AdminSite


class PortalAdminSite(AdminSite):
    site_header = "Dogfood Portal administration"
    site_title = "Dogfood Portal admin"
    index_title = "Platform administration"

    def has_permission(self, request):
        user = request.user
        return bool(
            user.is_active
            and user.is_authenticated
            and getattr(user, "is_platform_admin", False)
        )


# One instance, imported by each app's admin module and by config.urls. Deliberately not
# `django.contrib.admin.site`: that default instance is never routed, so a model registered
# against it by habit is unreachable rather than quietly public.
portal_admin_site = PortalAdminSite(name="portal_admin")
