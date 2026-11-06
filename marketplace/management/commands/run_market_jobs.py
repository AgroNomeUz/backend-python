from django.core.management.base import BaseCommand

from marketplace.jobs import run_all


class Command(BaseCommand):
    help = (
        "Apply order deadlines and generate contract deliveries. "
        "Safe to run as often as you like; run it from cron every few minutes."
    )

    def handle(self, *args, **options):
        for name, count in run_all().items():
            self.stdout.write(f"{name}: {count}")
