from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Organization, Region, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username", "phone", "email", "full_name", "organization",
        "must_change_password", "is_staff",
    )
    list_filter = UserAdmin.list_filter + ("must_change_password",)
    search_fields = UserAdmin.search_fields + ("phone", "full_name")
    list_select_related = ("organization",)
    # `permissions` are org-scoped and unrelated to Django's own auth
    # permissions above — hence the separate section.
    fieldsets = UserAdmin.fieldsets + (
        ("Contact", {"fields": ("phone", "full_name", "telegram")}),
        (
            "Organization",
            {"fields": ("organization", "permissions", "must_change_password")},
        ),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Contact", {"fields": ("phone", "full_name", "telegram")}),
        ("Organization", {"fields": ("organization", "permissions")}),
    )


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")
    ordering = ("name",)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "name", "region", "owner", "entity_type", "phone", "email",
        "is_verified", "created_at",
    )
    list_editable = ("is_verified",)
    list_select_related = ("region", "owner")
    search_fields = ("name", "tax_number", "owner__username")
    list_filter = ("region", "is_verified", "entity_type")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("owner",)
