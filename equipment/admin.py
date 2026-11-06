from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import (
    Asset,
    Booking,
    BookingItem,
    BookingStatusHistory,
    EquipmentCategory,
    EquipmentModel,
    EquipmentModelCompatibility,
    ExternalReference,
    Manufacturer,
    PricingRule,
)


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = ["name", "country", "website"]
    search_fields = ["name"]


@admin.register(EquipmentCategory)
class EquipmentCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "parent", "is_self_propelled"]
    list_filter = ["is_self_propelled"]
    prepopulated_fields = {"slug": ("name",)}


class EquipmentModelCompatibilityInline(admin.TabularInline):
    model = EquipmentModelCompatibility
    fk_name = "primary_model"
    extra = 0


@admin.register(EquipmentModel)
class EquipmentModelAdmin(admin.ModelAdmin):
    list_display = ["__str__", "category", "engine_power_kw", "is_self_propelled", "fuel_type", "hitch_category"]
    list_filter = ["category", "is_self_propelled", "fuel_type", "hitch_category", "manufacturer"]
    search_fields = ["name", "manufacturer__name"]
    inlines = [EquipmentModelCompatibilityInline]


@admin.register(Asset)
class AssetAdmin(GISModelAdmin):
    list_display = [
        "__str__", "organization", "operational_status",
        "ownership_status", "current_meter_hours", "manufacture_year",
    ]
    list_filter = ["operational_status", "ownership_status", "organization"]
    search_fields = ["serial_number", "vin_or_pin", "internal_inventory_number"]


@admin.register(PricingRule)
class PricingRuleAdmin(admin.ModelAdmin):
    list_display = [
        "__str__", "pricing_unit", "price", "currency",
        "includes_operator", "includes_fuel", "valid_from", "valid_to",
    ]
    list_filter = ["pricing_unit", "currency", "includes_operator"]


class BookingItemInline(admin.TabularInline):
    model = BookingItem
    extra = 0


class BookingStatusHistoryInline(admin.TabularInline):
    model = BookingStatusHistory
    extra = 0
    readonly_fields = ["changed_at"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = [
        "__str__", "customer_organization", "provider_organization", "status",
        "start_at", "end_at", "total_amount", "currency",
    ]
    list_filter = ["status", "provider_organization"]
    search_fields = ["customer_organization__name"]
    inlines = [BookingItemInline, BookingStatusHistoryInline]


@admin.register(ExternalReference)
class ExternalReferenceAdmin(admin.ModelAdmin):
    list_display = [
        "external_system", "external_id", "content_type", "object_id", "updated_at",
    ]
    list_filter = ["external_system"]
    search_fields = ["external_id"]
