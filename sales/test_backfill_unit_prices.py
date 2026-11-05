# SPDX-License-Identifier: AGPL-3.0-or-later
"""`manage.py backfill_unit_prices`: refreshes imported lines from QuickBooks' tables without touching documents
staff already worked on in the app (Review finding 1: a QB held receipt edited here loses Document.source_id but
its lines keep theirs, so a by-line backfill would put QB's old price back under the receipt's new subtotal)."""
import io
import json
import os
import sqlite3
import tempfile
from decimal import Decimal as D

from django.core.management import call_command

from .models import Document, Line
from .test_returns import ReturnTestCase


class BackfillUnitPricesTests(ReturnTestCase):
    def source_db(self, folder):
        path = os.path.join(folder, 'current.sqlite')
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE receipt (SID TEXT, DiscPercent TEXT)')
        con.execute('CREATE TABLE receipt_line (SID TEXT, ItemPos INTEGER, Qty TEXT, Price TEXT, DiscountPercent TEXT, RealPrice TEXT)')
        con.execute('CREATE TABLE sales_order_line (SID TEXT, ItemPos INTEGER, Qty TEXT, Price TEXT, DiscountPercent TEXT)')
        con.executemany('INSERT INTO receipt VALUES (?,?)', [('QBSALE', '5'), ('QBHELD', '0')])
        con.executemany('INSERT INTO receipt_line VALUES (?,?,?,?,?,?)', [
            ('QBSALE', 1, '1', '127.4625', '25', '6.37'),   # imported under the old scheme: needs the exact price + share
            ('QBHELD', 1, '1', '100', '0', '0'),            # the held receipt staff later edited in the app
            ('QBORDER', 1, '1', '555', '0', '0'),           # same SID/position as the ORDER line below, wrong table
        ])
        con.executemany('INSERT INTO sales_order_line VALUES (?,?,?,?,?)', [('QBORDER', 1, '2', '314.955', '10')])
        con.commit()
        con.close()
        return path

    def imported(self, source_id, kind, status, price, **fields):
        doc = Document.objects.create(source_id=source_id, kind=kind, status=status, customer=self.customer,
                                      customer_name=self.customer.full_name, was_completed=status == 'completed', **fields)
        Line.objects.create(document=doc, source_id=f'{source_id}:1', position=1, item=self.chair, sku='6045', item_name='TEST CHAIR',
                            description='TEST CHAIR', quantity=1, price=D(price), tax_rate=D('7'))
        return doc

    def run_backfill(self, path, commit):
        out = io.StringIO()
        call_command('backfill_unit_prices', path, commit=commit, stdout=out)
        return json.loads(out.getvalue())['counts']

    def test_only_documents_that_still_carry_their_qb_identity_are_refreshed(self):
        sale = self.imported('QBSALE', 'sale', 'completed', '127.46', subtotal=D('127.46'), total=D('129.57'))
        order = self.imported('QBORDER', 'order', 'open', '314.96')
        held = self.imported('QBHELD', 'sale', 'held', '100', subtotal=D('100'), total=D('107'))
        held_line = held.lines.get()
        # Staff work on the QB held receipt here: it becomes an app receipt (source_id cleared), rung at 80.
        held = self.save([dict(id=held_line.pk, item_id=self.chair.pk, quantity='1', price='80')], [], action='hold',
                         token=held.token, version=held.version)
        self.assertIsNone(held.source_id)
        self.assertEqual(held.lines.get().source_id, 'QBHELD:1')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as folder:
            path = self.source_db(folder)
            dry = self.run_backfill(path, commit=False)
            self.assertEqual(dry['current.sqlite:receipt_line'], dict(source_lines=3, updated=1))
            self.assertEqual(dry['current.sqlite:sales_order_line'], dict(source_lines=1, updated=1))
            self.assertEqual(dry['current.sqlite:receipt'], dict(source_receipts=2, updated=1))
            self.assertEqual(Line.objects.get(pk=sale.lines.get().pk).price, D('127.46'))  # dry run changed nothing
            first = self.run_backfill(path, commit=True)
            self.assertEqual(first, dry)
            again = self.run_backfill(path, commit=True)  # repeating finds nothing left to do
            self.assertEqual({k: v['updated'] for k, v in again.items()}, {'current.sqlite:receipt_line': 0, 'current.sqlite:sales_order_line': 0, 'current.sqlite:receipt': 0})
        line = Line.objects.get(pk=sale.lines.get().pk)
        self.assertEqual((line.price, line.discount_percent, line.receipt_discount, line.amount, line.net_amount),
                         (D('127.4625'), D('25'), D('6.37'), D('127.46'), D('121.09')))
        self.assertEqual(Document.objects.get(pk=sale.pk).discount_percent, D('5'))
        order_line = order.lines.get()
        self.assertEqual((order_line.price, order_line.discount_percent), (D('314.9550'), D('10')))  # from the ORDER table, not the receipt row
        worked = Document.objects.get(pk=held.pk)
        self.assertEqual((worked.lines.get().price, worked.subtotal, worked.total), (D('80'), D('80'), D('85.60')))
