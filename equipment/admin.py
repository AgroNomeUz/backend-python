from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import (
    Asset,
    AssetEvent,
    AvailabilityPeriod,
    Booking,
    BookingItem,
    BookingStatusHistory,
    DepositRule,
    Document,
    EquipmentCategory,
    EquipmentModel,
    EquipmentModelCompatibility,
    ExternalReference,
    FaultReport,
    Inspection,
    MaintenanceRecord,
    Manufacturer,
    PricingRule,
    WorkOrder,
    WorkSession,
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


@admin.register(AvailabilityPeriod)
class AvailabilityPeriodAdmin(admin.ModelAdmin):
    list_display = ["asset", "period_type", "starts_at", "ends_at"]
    list_filter = ["period_type"]


@admin.register(PricingRule)
class PricingRuleAdmin(admin.ModelAdmin):
    list_display = [
        "__str__", "pricing_unit", "price", "currency",
        "includes_operator", "includes_fuel", "valid_from", "valid_to",
    ]
    list_filter = ["pricing_unit", "currency", "includes_operator"]


@admin.register(DepositRule)
class DepositRuleAdmin(admin.ModelAdmin):
    list_display = ["organization", "asset", "amount", "currency", "is_refundable"]


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


class WorkSessionInline(admin.TabularInline):
    model = WorkSession
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = [
        "__str__", "operation_type", "status",
        "planned_start_at", "planned_area_ha",
    ]
    list_filter = ["status", "operation_type"]
    inlines = [WorkSessionInline]


@admin.register(WorkSession)
class WorkSessionAdmin(GISModelAdmin):
    list_display = [
        "__str__", "asset", "operator", "started_at",
        "area_completed_ha", "fuel_used_l", "source",
    ]
    list_filter = ["source"]


@admin.register(MaintenanceRecord)
class MaintenanceRecordAdmin(admin.ModelAdmin):
    list_display = [
        "__str__", "maintenance_type", "started_at",
        "completed_at", "cost", "currency",
    ]
    list_filter = ["maintenance_type"]


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = [
        "__str__", "inspection_type", "condition_status", "inspected_at", "inspector",
    ]
    list_filter = ["inspection_type", "condition_status"]


@admin.register(FaultReport)
class FaultReportAdmin(admin.ModelAdmin):
    list_display = ["__str__", "severity", "status", "reported_at", "reported_by"]
    list_filter = ["severity", "status"]


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = [
        "document_type", "organization", "uploaded_by", "uploaded_at", "expires_at",
    ]
    list_filter = ["document_type"]


@admin.register(AssetEvent)
class AssetEventAdmin(GISModelAdmin):
    list_display = ["asset", "event_type", "occurred_at", "booking", "work_order"]
    list_filter = ["event_type"]
    ordering = ["-occurred_at"]


@admin.register(ExternalReference)
class ExternalReferenceAdmin(admin.ModelAdmin):
    list_display = [
        "external_system", "external_id", "content_type", "object_id", "updated_at",
    ]
    list_filter = ["external_system"]
    search_fields = ["external_id"]
