from django.core.management.base import BaseCommand, CommandError

from billing.reconciliation import reconcile


class Command(BaseCommand):
    help = "Check the ledger against itself. Exits non-zero if anything disagrees."

    def handle(self, *args, **options):
        problems = reconcile()
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} problem(s) found")
        self.stdout.write("The ledger reconciles.")
