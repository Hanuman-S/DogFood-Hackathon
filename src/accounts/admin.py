"""Admin registrations for accounts, on the gated portal admin site."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from accounts.models import ApiToken, User
from core.admin_site import portal_admin_site


@admin.register(User, site=portal_admin_site)
class UserAdmin(DjangoUserAdmin):
    """Email-based user admin.

    `UserAdmin`'s defaults all reference `username`, which this model does not have, so every
    fieldset has to be restated rather than inherited.
    """

    ordering = ["email"]
    list_display = [
        "email",
        "display_name",
        "is_platform_admin",
        "can_create_events",
        "is_active",
    ]
    list_filter = ["is_platform_admin", "can_create_events", "is_active", "is_staff"]
    search_fields = ["email", "display_name", "external_id"]
    readonly_fields = ["created_at", "updated_at", "last_login"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("display_name", "external_id")}),
        (
            "Platform roles",
            {
                "fields": ("is_platform_admin", "can_create_events"),
                "description": (
                    "Participant, judge and organizer are per-event roles and live on "
                    "EventMembership, not here."
                ),
            },
        ),
        (
            "Django permissions",
            {
                "classes": ("collapse",),
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
            },
        ),
        ("Timestamps (UTC)", {"fields": ("created_at", "updated_at", "last_login")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "display_name", "password1", "password2"),
            },
        ),
        ("Platform roles", {"fields": ("is_platform_admin", "can_create_events")}),
    )


@admin.register(ApiToken, site=portal_admin_site)
class ApiTokenAdmin(admin.ModelAdmin):
    """Read-mostly view of issued tokens.

    There is no way to see or set a token's value here, because the plaintext is not stored.
    An admin can see that a token exists, who owns it, whether it has been used and whether
    it is revoked -- which is everything an operator needs and nothing they could leak.
    """

    list_display = ["name", "user", "created_at", "last_used_at", "revoked_at"]
    list_filter = ["revoked_at"]
    search_fields = ["name", "user__email"]
    readonly_fields = ["token_hash", "created_at", "last_used_at"]
    autocomplete_fields = ["user"]
