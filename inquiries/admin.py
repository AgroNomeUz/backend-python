from django.contrib import admin

from .models import Inquiry


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = [
        "listing",
        "customer_organization",
        "provider_organization",
        "created_by",
        "read_at",
        "read_by",
        "created_at",
    ]
    list_filter = ["created_at"]
    search_fields = [
        "message",
        "listing__title",
        "customer_organization__name",
        "provider_organization__name",
    ]
    raw_id_fields = [
        "listing",
        "customer_organization",
        "provider_organization",
        "created_by",
        "read_by",
    ]
    readonly_fields = ["created_at", "updated_at"]
