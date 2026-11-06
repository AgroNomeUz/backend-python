from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from billing.reconciliation import needs_a_person, reconcile


class Command(BaseCommand):
    help = (
        "Check the ledger against itself and list what needs a person. "
        "Exits non-zero if either finds anything."
    )

    def handle(self, *args, **options):
        # One snapshot, so a release landing mid-check is not a false alarm. Here
        # and not in `reconcile()`, which tests call inside a transaction already
        # under way, where SET TRANSACTION is an error.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            problems = [*reconcile(), *needs_a_person()]
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} problem(s) found")
        self.stdout.write("The ledger reconciles.")
