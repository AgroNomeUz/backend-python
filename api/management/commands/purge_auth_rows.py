from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import PhoneOtp, RefreshToken


class Command(BaseCommand):
    help = "Delete expired refresh tokens and phone codes older than a day."

    def handle(self, *args, **options):
        now = timezone.now()
        tokens, _ = RefreshToken.objects.filter(expires_at__lt=now).delete()
        # A day outlives every use of the row: the code, the 15-minute signup
        # token that points at it, and the hourly limits counted from it.
        otps, _ = PhoneOtp.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
        self.stdout.write(f"Deleted {tokens} refresh tokens and {otps} phone OTPs.")
