# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill imported sales orders' whole-order discount without a rebuild. Dry run by default.

Before the importer did not read sales_order.DiscAmount / DiscPercent / DiscountLevel, so every imported
discounted order (170 in the 10/9 backup) stored discount 0 and total = QB's discounted total: the printout showed
Subtotal + Tax != TOTAL and reports read the lines gross. This sets Document.discount / discount_percent /
discount_reason from the backup by source SID and spreads the amount over the lines (Line.receipt_discount) exactly
as the importer now does (sales.rebuild.order_discount_fields / spread_order_discounts). Totals are untouched: QB's
Total already had the discount taken off.
"""
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from sales.models import Document
from sales.rebuild import order_discount_fields, spread_order_discounts


class Command(BaseCommand):
    help = 'Backfill imported sales orders\' order-level discount (amount, percent, reason, line shares) from a current.sqlite.'

    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        try:
            with closing(sqlite3.connect(Path(options['path']).resolve().as_uri() + '?mode=ro', uri=True)) as source:
                source.row_factory = sqlite3.Row
                wanted = {r['SID']: order_discount_fields(r) for r in source.execute(
                    'SELECT SID, DiscAmount, DiscPercent, DiscountLevel FROM sales_order WHERE selected = 1 AND (DiscAmount != 0 OR DiscPercent != 0 OR (DiscountLevel IS NOT NULL AND DiscountLevel != \'\'))')}
        except sqlite3.Error as exc:
            raise CommandError(f'Cannot read sales order discounts: {exc}') from exc
        with transaction.atomic():
            orders = list(Document.objects.filter(kind='order', source_id__in=wanted).only('pk', 'source_id', 'discount', 'discount_percent', 'discount_reason'))
            changed = []
            for order in orders:
                fields = wanted[order.source_id]
                if (order.discount, order.discount_percent, order.discount_reason) != (fields['discount'], fields['discount_percent'], fields['discount_reason']):
                    for key, value in fields.items():
                        setattr(order, key, value)
                    changed.append(order)
            Document.objects.bulk_update(changed, ['discount', 'discount_percent', 'discount_reason'], batch_size=1000)
            lines_changed = spread_order_discounts(Document.objects.filter(pk__in=[o.pk for o in orders]).exclude(discount=0))
            # Sanity: every backfilled order's lines net to subtotal - discount.
            off = []
            for order in Document.objects.filter(pk__in=[o.pk for o in orders]).exclude(discount=0).prefetch_related('lines'):
                net = sum((l.net_amount for l in order.lines.all()), Decimal('0'))
                if net != (order.subtotal or Decimal('0')) - order.discount:
                    off.append(dict(order=order.number, subtotal=str(order.subtotal), discount=str(order.discount), net_lines=str(net)))
            if not options['commit']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps({'committed': options['commit'], 'source_orders_with_discount': len(wanted), 'matched': len(orders),
                                      'orders_updated': len(changed), 'line_shares_updated': lines_changed, 'orders_not_netting_to_subtotal_minus_discount': off}, default=str))
