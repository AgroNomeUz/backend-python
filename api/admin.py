from django.conf import settings
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError

from .auth import login_failures_key
from .models import PhoneOtp, RefreshToken


class LimitedAdminLoginForm(AdminAuthenticationForm):
    """Guesses at `/admin` count against the same limit as `/auth/login`."""

    def clean(self):
        key = login_failures_key(self.cleaned_data.get("username"))
        failures = cache.get(key, 0)
        if failures >= settings.LOGIN_MAX_FAILURES:
            raise ValidationError("Too many failed attempts. Try again later.")

        try:
            cleaned_data = super().clean()
        except ValidationError:
            cache.set(key, failures + 1, settings.LOGIN_LOCKOUT_SECONDS)
            raise
        # A blank password raises nothing here, and must not clear the count.
        if self.user_cache is not None:
            cache.delete(key)
        return cleaned_data


admin.site.login_form = LimitedAdminLoginForm


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
    readonly_fields = ("user", "created_at", "expires_at")
    exclude = ("token_hash",)

    def has_add_permission(self, request):
        return False
