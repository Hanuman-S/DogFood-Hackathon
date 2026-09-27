from django.contrib import admin

from platform_admin.site import database_admin

from .models import Publication, ResultSnapshot


class ReadOnlyAdmin(admin.ModelAdmin):
    """Results are records: nobody edits or deletes them here, admins included."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ResultSnapshot, site=database_admin)
class ResultSnapshotAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "event", "kind", "method", "method_version", "created_by_email", "input_hash"]
    list_filter = ["kind", "method"]


@admin.register(Publication, site=database_admin)
class PublicationAdmin(ReadOnlyAdmin):
    list_display = ["published_at", "event", "snapshot", "published_by_email", "unpublished_at"]
