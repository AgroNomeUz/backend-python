from django.core.management.base import BaseCommand, CommandError

from marketplace.jobs import run_all


class Command(BaseCommand):
    help = (
        "Apply order deadlines and generate contract deliveries. "
        "Safe to run as often as you like; the scheduler service runs it every "
        "five minutes. Exits non-zero if any order or contract failed."
    )

    def handle(self, *args, **options):
        counts = run_all()
        for name, count in counts.items():
            self.stdout.write(f"{name}: {count}")
        if counts["failed"]:
            raise CommandError(f"{counts['failed']} market job(s) failed, see the log")
