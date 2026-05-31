from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Organization, Region, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "organization", "is_staff")
    fieldsets = UserAdmin.fieldsets + (
        ("Organization", {"fields": ("organization",)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Organization", {"fields": ("organization",)}),
    )


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")
    ordering = ("name",)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "region", "owner", "phone", "email", "created_at")
    list_select_related = ("region", "owner")
    search_fields = ("name", "tax_number", "owner__username")
    list_filter = ("region",)
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("owner",)
