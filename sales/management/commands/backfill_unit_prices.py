# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refresh already-imported receipt/order lines with QuickBooks' exact unit prices and discount shares, without a
full rebuild. Dry run unless --commit.

Per line (by source id, same helper as the importer: `sales.rebuild.line_money_fields`): price to 4 decimals as QB keeps
% line discounts, the % off (DiscountPercent), the line's share of the receipt discount (RealPrice) and QB's recorded
line total only where even the 4-decimal price cannot reproduce it. Per receipt: the receipt discount percent
(DiscPercent, 2 places). Run after migration 0033 on every database that was imported before it:
    manage.py backfill_unit_prices current.sqlite held.sqlite --commit
"""
import json
import sqlite3

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F

from sales.models import Document, Line
from sales.rebuild import line_money_fields, percent2

LINE_FIELDS = ['price', 'recorded_amount', 'discount_percent', 'receipt_discount']
# (QB table, its columns, the document kind those lines belong to)
SOURCES = [('receipt_line', ['SID', 'ItemPos', 'Qty', 'Price', 'DiscountPercent', 'RealPrice'], 'sale'),
           ('sales_order_line', ['SID', 'ItemPos', 'Qty', 'Price', 'DiscountPercent'], 'order')]


class Command(BaseCommand):
    help = "Fill Line.price (4 decimals), discount_percent, receipt_discount, recorded_amount and Document.discount_percent from current.sqlite (and held.sqlite)."

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
                name = path.rsplit('/', 1)[-1]
                for table, columns, kind in SOURCES:
                    if table not in tables:
                        continue
                    wanted = {}
                    for row in con.execute(f'SELECT {", ".join(f"[{c}]" for c in columns)} FROM "{table}"'):
                        r = dict(zip(columns, row))
                        wanted[f'{r["SID"]}:{r["ItemPos"]}'] = line_money_fields(r)
                    changed = []
                    # Only lines whose document still IS the QuickBooks document: working on an imported held receipt
                    # clears Document.source_id (services.save_document) but its lines keep theirs, and those lines now
                    # hold what staff rang here, not what QB had (Review finding 1). The line's SID must be
                    # the document's own source id, and the table must match the document kind.
                    lines = (Line.objects.filter(source_id__in=list(wanted), document__kind=kind, document__source_id__isnull=False)
                             .annotate(document_source=F('document__source_id')).only('pk', 'source_id', *LINE_FIELDS))
                    for line in lines.iterator(chunk_size=5000):
                        if line.source_id.rsplit(':', 1)[0] != line.document_source:
                            continue
                        fields = wanted[line.source_id]
                        if any(getattr(line, k) != v for k, v in fields.items()):
                            for k, v in fields.items():
                                setattr(line, k, v)
                            changed.append(line)
                    Line.objects.bulk_update(changed, LINE_FIELDS, batch_size=2000)
                    counts[f'{name}:{table}'] = dict(source_lines=len(wanted), updated=len(changed))
                if 'receipt' in tables:
                    wanted = {sid: percent2(pct) for sid, pct in con.execute('SELECT SID, DiscPercent FROM receipt')}
                    changed = []
                    for doc in Document.objects.filter(kind='sale', source_id__in=list(wanted)).only('pk', 'source_id', 'discount_percent').iterator(chunk_size=5000):
                        if doc.discount_percent != wanted[doc.source_id]:
                            doc.discount_percent = wanted[doc.source_id]
                            changed.append(doc)
                    Document.objects.bulk_update(changed, ['discount_percent'], batch_size=2000)
                    counts[f'{name}:receipt'] = dict(source_receipts=len(wanted), updated=len(changed))
                con.close()
            if not options['commit']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps(dict(committed=options['commit'], counts=counts), indent=1))
