from django.contrib import admin

from .models import Favorite


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ["listing", "user", "organization", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["listing__title", "user__username", "organization__name"]
    raw_id_fields = ["listing", "user", "organization"]
    readonly_fields = ["created_at"]
