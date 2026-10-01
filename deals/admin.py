from django.contrib import admin

from .models import Deal, Review


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "deal_type",
        "status",
        "provider_organization",
        "customer_organization",
        "completed_at",
        "created_at",
    ]
    list_filter = ["deal_type", "status"]
    search_fields = ["provider_organization__name", "customer_organization__name"]
    raw_id_fields = ["listing", "inquiry", "provider_organization", "customer_organization", "created_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ["id", "deal", "author_organization", "rating", "created_at"]
    list_filter = ["rating"]
    search_fields = ["author_organization__name", "comment"]
    raw_id_fields = ["deal", "author_organization", "created_by"]
    readonly_fields = ["created_at"]
