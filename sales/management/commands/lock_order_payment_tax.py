# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lock each customer-order payment's sales-tax figures as the tax ledger reports them today (the tax
inside a deposit or payment is fixed on the day the money is received; a later edit to the order changes only the
balance and the tax still due, booked when that balance is paid). Dry run unless --commit.

Stores tax, taxable, non-taxable, the Account-charged taxable part and the report section on every QuickBooks deposit /
deposit-refund receipt linked to an order and on every payment recorded in this app (sales.models.OrderPaymentTaxLock),
computed by the ledger's own rule (reports.tax_ledger.order_tax_locks), so Sales Tax and Daily Totals for every past day
stay identical to the cent. Idempotent: a payment that already carries figures is never overwritten. The importer runs
the same step after a load (sales.rebuild.rebuild_from_backup); run this once on every database that was loaded before
migration 0039:
    manage.py lock_order_payment_tax --commit
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from reports.tax_ledger import lock_all_orders


class Command(BaseCommand):
    help = "Store each order payment's sales-tax figures as reported today. Dry run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='write the figures (default: dry run, rolled back)')
        parser.add_argument('--dry-run', action='store_true', help='the default; shown for clarity')

    def handle(self, *args, **options):
        commit = options['commit'] and not options['dry_run']
        with transaction.atomic():
            counts = lock_all_orders(lambda message: self.stderr.write(message))
            if not commit:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps(dict(committed=commit, **counts)))
