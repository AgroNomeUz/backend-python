"""
The two ledger rules that must hold whatever the application does.

  * **Append-only.** Correcting a mistake is a new, opposite transaction. A
    trigger refuses UPDATE and DELETE outright, so a shell session, a future
    bug or a stray bulk update cannot quietly change history. TRUNCATE is not
    blocked: row triggers do not see it, and the test runner needs it.
  * **Balanced.** A constraint trigger, deferred to commit because the legs of
    a transaction are inserted one row at a time.
"""

from django.db import migrations

SQL = """
CREATE FUNCTION billing_refuse_ledger_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'The ledger is append-only: % on % refused', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_ledgerentry_append_only
    BEFORE UPDATE OR DELETE ON billing_ledgerentry
    FOR EACH ROW EXECUTE FUNCTION billing_refuse_ledger_change();

CREATE TRIGGER billing_ledgertransaction_append_only
    BEFORE UPDATE OR DELETE ON billing_ledgertransaction
    FOR EACH ROW EXECUTE FUNCTION billing_refuse_ledger_change();

CREATE FUNCTION billing_check_transaction_balanced() RETURNS trigger AS $$
DECLARE
    total numeric;
BEGIN
    SELECT SUM(amount) INTO total
      FROM billing_ledgerentry
     WHERE transaction_id = NEW.transaction_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'Ledger transaction % is unbalanced by %', NEW.transaction_id, total;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER billing_ledgerentry_balanced
    AFTER INSERT ON billing_ledgerentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION billing_check_transaction_balanced();
"""

REVERSE_SQL = """
DROP TRIGGER billing_ledgerentry_balanced ON billing_ledgerentry;
DROP FUNCTION billing_check_transaction_balanced();
DROP TRIGGER billing_ledgertransaction_append_only ON billing_ledgertransaction;
DROP TRIGGER billing_ledgerentry_append_only ON billing_ledgerentry;
DROP FUNCTION billing_refuse_ledger_change();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=REVERSE_SQL),
    ]
