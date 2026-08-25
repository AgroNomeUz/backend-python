from django.contrib import admin

from .models import Listing, ListingImage


class ListingImageInline(admin.TabularInline):
    model = ListingImage
    extra = 0
    fields = ["file", "sort_order", "is_primary"]


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "organization",
        "listing_type",
        "status",
        "price",
        "price_unit",
        "region",
        "created_at",
    ]
    list_filter = ["status", "listing_type", "price_unit", "region"]
    search_fields = ["title", "description", "organization__name"]
    raw_id_fields = ["organization", "asset", "created_by"]
    readonly_fields = ["published_at", "created_at", "updated_at"]
    inlines = [ListingImageInline]
