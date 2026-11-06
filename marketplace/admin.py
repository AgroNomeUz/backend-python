"""
The catalogue is edited here, since no API can write it. Orders, contracts and
disputes are read-only: they carry money, and changing one by hand would skip
the state machine and the ledger. Disputes are settled through
`POST /market/disputes/{id}/resolve`.
"""

from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import (
    ContractTerms,
    Offer,
    Order,
    OrderDispute,
    Product,
    ProductCategory,
    PurchaseRequest,
    Quote,
    SupplyContract,
)


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "parent", "sort_order"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "default_unit", "eppo_code", "is_regulated", "is_active"]
    list_filter = ["category", "is_regulated", "is_active"]
    search_fields = ["name", "slug", "eppo_code"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "organization",
        "product",
        "status",
        "price",
        "quantity_available",
        "available_until",
    ]
    list_filter = ["status", "offer_type"]
    search_fields = ["title", "organization__name"]
    raw_id_fields = ["organization", "created_by", "crop_zone"]


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(ReadOnlyAdmin):
    list_display = ["id", "customer_organization", "product", "quantity", "unit", "status"]
    list_filter = ["status", "is_recurring"]


@admin.register(Quote)
class QuoteAdmin(ReadOnlyAdmin):
    list_display = ["id", "request", "provider_organization", "unit_price", "status"]
    list_filter = ["status"]


@admin.register(Order)
class OrderAdmin(ReadOnlyAdmin):
    list_display = [
        "id",
        "status",
        "provider_organization",
        "customer_organization",
        "product",
        "subtotal",
        "delivery_date",
    ]
    list_filter = ["status"]
    search_fields = ["provider_organization__name", "customer_organization__name"]


@admin.register(OrderDispute)
class OrderDisputeAdmin(ReadOnlyAdmin):
    list_display = ["id", "order", "status", "reason", "disputed_amount", "refund_amount"]
    list_filter = ["status", "reason"]


@admin.register(SupplyContract)
class SupplyContractAdmin(ReadOnlyAdmin):
    list_display = [
        "id",
        "status",
        "provider_organization",
        "customer_organization",
        "product",
        "termination_date",
    ]
    list_filter = ["status"]


@admin.register(ContractTerms)
class ContractTermsAdmin(ReadOnlyAdmin):
    list_display = ["id", "contract", "version", "status", "unit_price", "start_date", "end_date"]
    list_filter = ["status"]
