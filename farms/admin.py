from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import CropZone, Farm, FarmCluster, Field, FieldBoundary, Season


@admin.register(FarmCluster)
class FarmClusterAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "created_at"]
    list_select_related = ["organization"]
    search_fields = ["name"]


@admin.register(Farm)
class FarmAdmin(GISModelAdmin):
    list_display = ["name", "organization", "cluster", "region"]
    list_select_related = ["organization", "cluster", "region"]
    search_fields = ["name"]
    list_filter = ["region"]


class FieldBoundaryInline(admin.TabularInline):
    """Boundary history, shown on the field and not editable here."""

    model = FieldBoundary
    extra = 0
    can_delete = False
    fields = ["valid_from", "valid_to", "area", "area_unit", "source", "created_by"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Field)
class FieldAdmin(admin.ModelAdmin):
    list_display = ["__str__", "farm", "organization", "area", "area_unit", "soil_type"]
    list_select_related = ["farm", "organization"]
    list_filter = ["farm__region"]
    search_fields = ["name", "farm__name"]
    inlines = [FieldBoundaryInline]


@admin.register(FieldBoundary)
class FieldBoundaryAdmin(GISModelAdmin):
    """History is append-only: viewable, never edited or deleted here."""

    list_display = ["field", "valid_from", "valid_to", "area", "area_unit", "source"]
    list_select_related = ["field", "field__farm"]
    list_filter = ["source"]
    search_fields = ["field__name", "field__farm__name"]
    readonly_fields = [
        "field", "geometry", "area", "area_unit", "valid_from", "valid_to",
        "source", "created_by", "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "start_date", "end_date"]
    list_select_related = ["organization"]
    search_fields = ["name"]


@admin.register(CropZone)
class CropZoneAdmin(GISModelAdmin):
    list_display = ["__str__", "crop_code", "season", "area", "area_unit", "planting_date"]
    list_select_related = ["field", "field__farm", "season"]
    list_filter = ["season", "crop_code"]
    search_fields = ["crop_name", "crop_code", "field__name"]
