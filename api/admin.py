from django.contrib import admin

from .models import PhoneOtp, RefreshToken


@admin.register(PhoneOtp)
class PhoneOtpAdmin(admin.ModelAdmin):
    """
    Read-only: these rows are credentials and rate-limit evidence, and
    editing one by hand would either hand out a login or clear a limit.
    The code itself is stored hashed and is deliberately not shown.
    """

    list_display = (
        "phone", "created_at", "expires_at", "attempts",
        "verified_at", "signup_claimed_at", "ip",
    )
    list_filter = ("created_at",)
    search_fields = ("phone",)
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RefreshToken)
class RefreshTokenAdmin(admin.ModelAdmin):
    """Revocable from here — the one edit that is legitimate."""

    list_display = ("user", "created_at", "expires_at", "is_revoked")
    list_filter = ("is_revoked",)
    list_select_related = ("user",)
    search_fields = ("user__username", "user__phone", "user__email")
    ordering = ("-created_at",)
    raw_id_fields = ("user",)
    readonly_fields = ("user", "token", "created_at", "expires_at")

    def has_add_permission(self, request):
        return False
