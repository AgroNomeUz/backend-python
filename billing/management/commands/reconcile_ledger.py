from django.core.management.base import BaseCommand, CommandError

from billing.reconciliation import needs_a_person, reconcile


class Command(BaseCommand):
    help = (
        "Check the ledger against itself and list what needs a person. "
        "Exits non-zero if either finds anything."
    )

    def handle(self, *args, **options):
        problems = [*reconcile(), *needs_a_person()]
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} problem(s) found")
        self.stdout.write("The ledger reconciles.")
