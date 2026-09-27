"""Tables exposed in the database admin (/admin/db/). Admins only."""

from django.contrib import admin

from accounts.models import ApiToken, User, UserSession
from core.models import AuditLog
from platform_admin.site import database_admin


@admin.register(User, site=database_admin)
class UserAdmin(admin.ModelAdmin):
    list_display = ["email", "name", "is_platform_admin", "can_create_events", "is_active", "date_joined", "last_login"]
    list_filter = ["is_platform_admin", "can_create_events", "is_active"]
    search_fields = ["email", "name"]
    ordering = ["email"]
    fields = ["email", "name", "is_platform_admin", "can_create_events", "is_active", "date_joined", "last_login"]
    readonly_fields = ["date_joined", "last_login"]

    def has_add_permission(self, request):
        # Accounts are created through the admin portal (/admin/), which sets a password
        # properly and writes an audit row.
        return False


@admin.register(ApiToken, site=database_admin)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ["prefix", "name", "user", "created_at", "last_used_at", "revoked_at"]
    list_filter = ["revoked_at"]
    search_fields = ["prefix", "name", "user__email"]
    readonly_fields = ["user", "prefix", "digest", "created_at", "last_used_at"]
    fields = ["user", "name", "prefix", "digest", "created_at", "last_used_at", "revoked_at"]

    def has_add_permission(self, request):
        return False  # a token made here would have no raw secret anyone could use


@admin.register(UserSession, site=database_admin)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ["user", "device", "ip", "created_at", "last_seen_at"]
    search_fields = ["user__email", "ip"]
    readonly_fields = ["user", "session_key", "ip", "user_agent", "created_at", "last_seen_at"]

    def has_add_permission(self, request):
        return False


@admin.register(AuditLog, site=database_admin)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only, even for admins: an audit trail an admin could edit would prove nothing."""

    list_display = ["created_at", "actor_email", "action", "subject", "ip"]
    list_filter = ["action"]
    search_fields = ["actor_email", "subject", "ip"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
