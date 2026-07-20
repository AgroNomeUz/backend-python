from django.contrib import admin

from .models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "actor", "action", "target_repr")
    list_filter = ("action", "created_at", "organization")
    search_fields = ("target_repr", "actor__username", "organization__name")
    date_hierarchy = "created_at"
    readonly_fields = (
        "public_id",
        "organization",
        "actor",
        "action",
        "content_type",
        "object_id",
        "target_repr",
        "changes",
        "context",
        "created_at",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
