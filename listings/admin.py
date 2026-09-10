from django.contrib import admin

from .models import Listing, ListingImage, ListingView


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


@admin.register(ListingView)
class ListingViewAdmin(admin.ModelAdmin):
    """
    Read-only: these rows are a counter, and an edited one is a wrong number
    with nothing to say it used to be right. `viewer_key` is a salted hash and
    is deliberately not searchable — it exists to be compared against itself.
    """

    list_display = ["listing", "organization", "viewed_on", "created_at"]
    list_filter = ["viewed_on"]
    list_select_related = ["listing", "organization"]
    search_fields = ["listing__title", "organization__name"]
    raw_id_fields = ["listing", "organization"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
