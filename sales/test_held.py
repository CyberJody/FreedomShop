# SPDX-License-Identifier: AGPL-3.0-or-later
"""Held receipts: QB POS held receipts imported, listed, shown, reprinted, deleted and unheld."""
import sqlite3
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import TestCase, override_settings
from django.utils import timezone

from customers.models import Customer
from . import rebuild
from .models import Document, Item, Line, StockMovement
from .services import save_document

SHA = 'ef' * 32


def write_held(path, rows, lines):
    """A held.sqlite like build_held.py writes: receipt/receipt_line with data_state, plus meta."""
    con = sqlite3.connect(path)
    for table in ('receipt', 'receipt_line'):
        cols = ['data_state'] + rebuild.COLUMNS[table]
        con.execute(f'CREATE TABLE {table} ({", ".join(f"[{c}]" for c in cols)})')
    con.execute('CREATE TABLE meta (key TEXT, value TEXT)')
    con.execute('INSERT INTO meta VALUES (?, ?)', ('source_sha256', SHA))
    for table, data in (('receipt', rows), ('receipt_line', lines)):
        cols = ['data_state'] + rebuild.COLUMNS[table]
        for values in data:
            values = {'data_state': 1, 'selected': 1, **values}
            con.execute(f'INSERT INTO {table} VALUES ({",".join("?" * len(cols))})', [values.get(c) for c in cols])
    con.commit()
    con.close()


class HeldImportTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='C1', full_name='CASEY EXAMPLE', phone='555-0102')
        self.item = Item.objects.create(source_id='I1', sku='9001', name='MWW83-1-HICK-BEAR/BAY', description='RUSTIC SWIVEL GLIDER RECLINER',
                                        price='2084.95', on_hand='3', tax_rate='7.000')
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'held.sqlite'
        write_held(self.path, [
            dict(SID='R1', ReceiptNum=121317, ReceiptType=0, ReceiptStatus=0, Workstation=8, BillToSID='C1', Store=1, TaxArea='Springfield', Cashier='Morgan',
                 Clerk='Morgan', SubTotal='2259.70', TaxAmount='158.18', TaxPercent='7', Total='2417.88', ReceiptDate='2026-07-18', ReceiptTime='16:28:09.312000',
                 ReceiptCustomer__CustSID='C1', ReceiptCustomer__IsCompany=0, ReceiptCustomer__CustFName='CASEY', ReceiptCustomer__CustLName='EXAMPLE'),
            dict(SID='R2', ReceiptNum=118310, ReceiptType=0, ReceiptStatus=0, Workstation=4, Store=1, Cashier='Morgan', SubTotal='0', TaxAmount='0', TaxPercent='7',
                 Total='0', ReceiptDate='2018-01-23', ReceiptTime='14:40:29.000000'),
        ], [
            dict(SID='R1', ItemPos=0, ItemSID='I1', Qty='1', Price='2084.95', TaxPercent='7', ReceiptItemDesc__ItemNo=9001, ReceiptItemDesc__Desc1='MWW83-1-HICK-BEAR/BAY',
                 ReceiptItemDesc__Desc2='RUSTIC SWIVEL GLIDER RECLINER'),
            dict(SID='R1', ItemPos=1, ItemSID='GONE', Qty='1', Price='174.756667', TaxPercent='7', ReceiptItemDesc__ItemNo=55, ReceiptItemDesc__Desc1='OLD ITEM'),
        ])

    def tearDown(self):
        self.dir.cleanup()

    def load(self):
        return rebuild.load_held_receipts(str(self.path), commit=True)

    def test_loads_held_receipts_like_qb(self):
        result = self.load()
        self.assertEqual((result['counts']['held_receipts'], result['counts']['held_receipt_lines'], result['counts']['held_receipt_prices_truncated']), (2, 2, 1))
        doc = Document.objects.get(source_id='R1')
        self.assertEqual((doc.status, doc.source_number, doc.customer, doc.workstation, doc.cashier, doc.total, doc.completed_at), ('held', None, self.customer, 8, 'Morgan', Decimal('2417.88'), None))
        self.assertEqual(timezone.localtime(doc.created_at).strftime('%Y-%m-%d %H:%M'), '2026-07-18 16:28')  # QB date, not the load date
        self.assertEqual(doc.number, '')  # Held receipts have no issued number.
        self.assertFalse(doc.imported)  # held receipts from QB are workable, not read-only history
        self.assertEqual([l.price for l in doc.lines.order_by('position')], [Decimal('2084.95'), Decimal('174.7567')])  # QB's price to 4 decimals, half-up
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(Item.objects.get(pk=self.item.pk).on_hand, Decimal('3'))

    def test_reload_replaces_untouched_held_receipts_only(self):
        self.load()
        worked = Document.objects.get(source_id='R2')
        worked.source_id = None  # already worked on in the app
        worked.save()
        result = self.load()
        self.assertEqual(result['counts']['held_receipts_replaced'], 1)
        self.assertEqual(Document.objects.filter(kind='sale', status='held').count(), 3)

    def test_dry_run_rolls_back(self):
        rebuild.load_held_receipts(str(self.path), commit=False)
        self.assertFalse(Document.objects.exists())

    def test_rebuild_reads_held_sqlite_beside_current(self):
        con = sqlite3.connect(':memory:')
        con.execute('CREATE TABLE receipt (SID TEXT)')
        self.assertFalse(rebuild.has_held(con))
        held, where = rebuild.held_source(con, str(self.path.with_name('current.sqlite')), {'source_sha256': SHA})
        self.assertEqual(where, self.path)
        held.close()
        dated = self.path.with_name('held-20260927.sqlite')
        self.path.rename(dated)
        held, where = rebuild.held_source(con, str(dated.with_name('current-20260927.sqlite')), {'source_sha256': SHA})
        self.assertEqual(where, dated)
        held.close()
        dated.rename(self.path)
        with self.assertRaises(rebuild.RebuildError):
            rebuild.held_source(con, str(self.path.with_name('current.sqlite')), {'source_sha256': 'aa' * 32})

    def test_unhold_and_complete_makes_a_normal_receipt(self):
        self.load()
        doc = Document.objects.get(source_id='R1')
        line = doc.lines.get(position=0)
        done = save_document(dict(token=str(doc.token), version=doc.version, action='complete', customer_id=self.customer.pk,
                                  lines=[dict(id=line.pk, item_id=self.item.pk, quantity='1', price='2084.95', tax_rate='7')],
                                  payments=[dict(method='cash', amount='2230.90')]), 'sale', doc.token)
        self.assertEqual((done.status, done.source_id, done.number), ('completed', None, '200000'))
        self.assertEqual(done.total, Decimal('2230.90'))  # 7% of 2084.95 = 145.9465 -> 145.95 (half-up)
        self.assertEqual(Item.objects.get(pk=self.item.pk).on_hand, Decimal('2'))
        self.assertTrue(StockMovement.objects.filter(document=done).exists())


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class HeldScreenTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='C1', full_name='DANA SAMPLE', phone='518-555-0100')
        now = timezone.now()
        self.recent = Document.objects.create(kind='sale', status='held', customer=self.customer, customer_name='DANA SAMPLE', subtotal='785.00',
                                              tax='54.90', total='839.90', workstation=4, cashier='Morgan', receipt_type=0, tax_location='Springfield')
        Line.objects.create(document=self.recent, position=0, item_name='JMA449-H-N', description='ALL HICKORY BUFFET', quantity='1', price='785.00', tax_rate='7')
        self.old = Document.objects.create(kind='sale', status='held', customer_name='', subtotal='0', tax='0', total='0')
        Document.objects.filter(pk=self.old.pk).update(created_at=now.replace(year=now.year - 8))
        self.done = Document.objects.create(kind='sale', status='completed', customer=self.customer, customer_name='DANA SAMPLE', subtotal='1', tax='0', total='1', completed_at=now)

    def test_list_defaults_to_year_to_date_with_qb_columns(self):
        page = self.client.get('/pos/held/')
        self.assertContains(page, 'DANA SAMPLE')
        self.assertContains(page, '1 record')
        for header in ('Receipt Type', 'Workstation', 'Cashier', 'Total Qty Sold', 'Payment'):
            self.assertContains(page, f'>{header}<')
        self.assertContains(self.client.get('/pos/held/?period=all'), '2 records')
        rows = self.client.get('/pos/held/?format=rows&offset=0').json()
        self.assertEqual(rows['total'], 1)
        self.assertEqual(rows['rows'][0]['cells'][2:8], ['Regular', 'Sales', '4', 'Morgan', 'DANA SAMPLE', '1'])

    def test_details_panel(self):
        page = self.client.get(f'/pos/held/{self.recent.token}/details/')
        for text in ('DANA SAMPLE', '518-555-0100', 'HELD', 'JMA449-H-N', '$839.90', 'Workstation 4', 'Print Receipt', 'Unhold'):
            self.assertContains(page, text)
        self.assertEqual(self.client.get(f'/pos/held/{self.done.token}/details/').status_code, 404)

    def test_reprint_pdf_is_stamped_held(self):
        response = self.client.get(f'/print/sale/{self.recent.token}/pdf/')
        self.assertEqual((response.status_code, response['Content-Type']), (200, 'application/pdf'))
        self.assertIn('Held-Receipt', response['Content-Disposition'])

    def test_delete_removes_only_held_receipts(self):
        response = self.client.post(f'/pos/held/{self.recent.token}/delete/', {'next': 'period=all'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('deleted=', response['Location'])
        self.assertIn('period=all', response['Location'])
        self.assertFalse(Document.objects.filter(pk=self.recent.pk).exists())
        self.assertFalse(Line.objects.filter(document_id=self.recent.pk).exists())
        response = self.client.post(f'/pos/held/{self.done.token}/delete/')
        self.assertIn('error=', response['Location'])
        self.assertTrue(Document.objects.filter(pk=self.done.pk).exists())
        self.assertEqual(self.client.get(f'/pos/held/{self.recent.token}/delete/').status_code, 405)
