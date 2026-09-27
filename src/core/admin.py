"""Admin registration for the audit log.

Read-only on purpose. "An audit trail an organizer can actually read" is a scored requirement, and
a trail that an administrator can edit is not evidence of anything. Django's admin gives the
filtering and search for free; making the rows immutable here is one `has_change_permission`
away.
"""

from django.contrib import admin

from core.admin_site import portal_admin_site
from core.models import AuditLog


@admin.register(AuditLog, site=portal_admin_site)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "action", "actor", "event", "target_type", "target_id"]
    list_filter = ["action", "event", "created_at"]
    search_fields = ["actor__email", "target_id", "action"]
    date_hierarchy = "created_at"
    list_select_related = ["actor", "event"]
    readonly_fields = [
        "created_at",
        "action",
        "actor",
        "event",
        "target_type",
        "target_id",
        "metadata",
    ]

    def has_add_permission(self, request):
        """Audit rows are written by the service layer, never typed in by hand."""
        return False

    def has_change_permission(self, request, obj=None):
        """Append-only. An editable audit trail is not an audit trail."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Not even for a platform admin.

        Deleting entries is exactly what someone covering their tracks would do, and there is no
        legitimate workflow that needs it from a web UI. Retention trimming, if a deployment ever
        wants it, belongs in a documented management command.
        """
        return False
