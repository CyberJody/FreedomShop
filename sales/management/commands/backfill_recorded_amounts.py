# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store QuickBooks' recorded line totals on already-imported lines, without a full rebuild. Dry run unless --commit.

Only lines whose QB price/cost had fractions of a cent get a value: those are the lines where qty x the whole-cent price
no longer equals the line total QB recorded. Same rule as the importer (`sales.models.qb_line_amount`).
"""
import json
import sqlite3

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from purchasing.models import PurchaseLine
from sales.models import Line, qb_line_amount
from sales.rebuild import dec, qty, whole, price4

# Receipt/order lines keep the unit price to 4 decimals; purchase lines keep costs in whole cents.
SOURCES = [('receipt_line', 'Price', Line, price4), ('sales_order_line', 'Price', Line, price4),
           ('purchase_order_line', 'Cost', PurchaseLine, whole), ('voucher_line', 'Cost', PurchaseLine, whole)]


class Command(BaseCommand):
    help = "Fill Line/PurchaseLine.recorded_amount from current.sqlite (and held.sqlite) by source id."

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='+', help='current.sqlite, optionally held.sqlite')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        counts = {}
        with transaction.atomic():
            for path in options['paths']:
                try:
                    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                except sqlite3.Error as exc:
                    raise CommandError(f'Cannot open {path}: {exc}') from exc
                for table, column, model, stored in SOURCES:
                    if table not in tables:
                        continue
                    wanted = {}
                    for sid, pos, raw_qty, raw in con.execute(f'SELECT SID, ItemPos, Qty, [{column}] FROM "{table}"'):
                        recorded = qb_line_amount(qty(raw_qty), dec(raw), stored(raw))
                        if recorded is not None:
                            wanted[f'{sid}:{pos}'] = recorded
                    rows = list(model.objects.filter(source_id__in=list(wanted)).only('pk', 'source_id', 'recorded_amount'))
                    for row in rows:
                        row.recorded_amount = wanted[row.source_id]
                    model.objects.bulk_update(rows, ['recorded_amount'], batch_size=2000)
                    counts[f'{path.rsplit("/", 1)[-1]}:{table}'] = dict(qb_fractional_lines=len(wanted), updated=len(rows))
                con.close()
            if not options['commit']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps(dict(committed=options['commit'], counts=counts), indent=1))
