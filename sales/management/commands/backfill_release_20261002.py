# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bring an already-loaded database up to the 10/2/2026 release without a full rebuild. Dry run unless --commit.

Fills, by QB source id, from the same current.sqlite the database was loaded from (reusing the importer's own code):
- customer QuickBooks Options: charge account, account limit, account balance, past due, last sale (customers 0007)
- deposit receipt -> customer order links (sales 0019)
Held receipts and QB line totals have their own commands: load_held_receipts, backfill_recorded_amounts.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from customers.models import Customer
from sales.models import Document
from sales.rebuild import Counts, Loader, RebuildError, account_fields, open_source, validate


class Command(BaseCommand):
    help = 'Backfill customer account fields and deposit-order links from current.sqlite. Dry run unless --commit.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to current.sqlite (the dataset this database was loaded from)')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        try:
            con = open_source(options['path'])
            meta = validate(con)
        except RebuildError as exc:
            raise CommandError(str(exc)) from exc
        counts = Counts()
        try:
            with transaction.atomic():
                loader = Loader(con, counts, None)
                fields = ['use_with_quickbooks', 'charge_account', 'account_limit', 'imported_account_balance', 'imported_past_due', 'imported_last_sale']
                by_sid = {c.source_id: c for c in Customer.objects.exclude(source_id=None).only('pk', 'source_id', *fields)}
                changed = []
                for r in loader.rows('customer'):
                    customer = by_sid.get(r['SID'])
                    if customer is None:
                        counts.bump('customers_not_in_app')
                        continue
                    values = account_fields(r)
                    if any(getattr(customer, k) != v for k, v in values.items()):
                        for k, v in values.items():
                            setattr(customer, k, v)
                        changed.append(customer)
                Customer.objects.bulk_update(changed, fields, batch_size=2000)
                counts['customers_updated'] = len(changed)
                orders_by_sid = dict(Document.objects.filter(kind='order').exclude(source_id=None).values_list('source_id', 'pk'))
                before = Document.objects.filter(deposit_order__isnull=False).count()
                loader.link_deposits(orders_by_sid)
                counts['deposit_links_before'] = before
                counts['deposit_links_after'] = Document.objects.filter(deposit_order__isnull=False).count()
                if not options['commit']:
                    transaction.set_rollback(True)
        finally:
            con.close()
        self.stdout.write(json.dumps(dict(committed=options['commit'], source_sha256=meta['source_sha256'], counts=counts), indent=1, default=str))
