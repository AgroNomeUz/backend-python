from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import CropSeason, Farm, Field


@admin.register(Farm)
class FarmAdmin(GISModelAdmin):
    list_display = ["name", "organization", "region"]
    list_select_related = ["organization", "region"]
    search_fields = ["name"]
    list_filter = ["region"]


@admin.register(Field)
class FieldAdmin(GISModelAdmin):
    list_display = ["__str__", "farm", "area_ha", "soil_type"]
    list_filter = ["farm__region"]
    search_fields = ["name", "farm__name"]


@admin.register(CropSeason)
class CropSeasonAdmin(admin.ModelAdmin):
    list_display = ["__str__", "crop_type", "season_year", "planting_date"]
    list_filter = ["season_year", "crop_type"]
