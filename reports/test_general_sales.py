# SPDX-License-Identifier: AGPL-3.0-or-later
"""General Sales report: column selection, grouping, whole cents, exports, direct print."""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from purchasing.models import Department, Vendor
from sales.models import Document, Item, Line, Printer

from . import general_sales, ny_periods, tabular_exports
from .tests import AdminModeClientTests
from accounts.signin_helpers import sign_in_register

NY = ZoneInfo('America/New_York')


def sale(number, when, customer='CUSTOMER', cashier='Morgan'):
    return Document.objects.create(kind='sale', status='completed', source_id=f'R{number}', source_number=number, receipt_type=0,
                                   customer_name=customer, cashier=cashier, completed_at=datetime(*when, 14, 0, tzinfo=NY), subtotal=0, tax=0, total=0)


def line(doc, position, item, qty, price, cost='0', original=None):
    return Line.objects.create(document=doc, position=position, item=item, sku=item.sku if item else '', item_name=item.name if item else 'Payment On Account',
                               description=item.description if item else 'Payment On Account', quantity=Decimal(qty), price=Decimal(price), tax_rate=7,
                               unit_cost=Decimal(cost), original_price=Decimal(original) if original else None, source_id=f'{doc.pk}-{position}')


class GeneralSalesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        dining = Department.objects.create(name='Dining Room', code='DIN')
        acc = Department.objects.create(name='ACCESSORIES', code='ACC')
        byler = Vendor.objects.create(company='DEMO WOODSHOP', code='BW')
        cls.chair = Item.objects.create(sku='BW-CHAIR', name='Chair', description='Oak chair', price=Decimal('200'), primary_vendor=byler, department=dining)
        cls.rug = Item.objects.create(sku='RUG', name='Rug', description='Wool rug', price=Decimal('60'), department=acc)
        cls.finish = Item.objects.create(sku='FINISH', name='Premium finish', description='Finish', price=Decimal('100'), item_type='noninventory', department=acc)
        r = sale(1, (2026, 9, 2), 'SMITH')
        line(r, 1, cls.chair, '2', '199.95', cost='100.00', original='199.95')
        line(r, 2, cls.rug, '1', '59.95', cost='20.004')
        r = sale(2, (2026, 9, 5), 'JONES', cashier='Kim')
        line(r, 1, cls.chair, '1', '1377.4525', cost='700', original='1449.95')   # discounted, fractional cents
        line(r, 2, cls.finish, '1', '100', cost='0')
        line(r, 3, None, '1', '500')   # payment on account: no item
        r = sale(3, (2026, 9, 9), 'SMITH')
        line(r, 1, cls.chair, '-1', '199.95', cost='100', original='199.95')   # return
        line(sale(4, (2026, 10, 1)), 1, cls.chair, '5', '199.95')   # out of range

    def period(self):
        return ny_periods.resolve('month', '2026-09', date(2026, 9, 30))

    def test_rows_and_totals_whole_cents(self):
        report = general_sales.build(self.period())
        self.assertFalse(report.grouped)
        self.assertEqual(len(report.groups), 1)
        rows = report.groups[0].rows
        self.assertEqual([r['sku'] for r in rows], ['BW-CHAIR', 'RUG', 'BW-CHAIR', 'FINISH', 'BW-CHAIR'])
        chair2 = rows[2]
        self.assertEqual((chair2['price'], chair2['amount'], chair2['cost'], chair2['margin']), (Decimal('1377.45'), Decimal('1377.45'), Decimal('700.00'), Decimal('677.45')))
        self.assertTrue(chair2['discounted'])
        self.assertFalse(rows[0]['discounted'])
        self.assertEqual(rows[1]['cost'], Decimal('20.00'))   # 20.004 truncated
        self.assertEqual(report.totals['quantity'], Decimal('4'))
        self.assertEqual(report.totals['amount'], Decimal('399.90') + Decimal('59.95') + Decimal('1377.45') + Decimal('100') - Decimal('199.95'))
        self.assertEqual(report.totals['margin'], report.totals['amount'] - report.totals['cost'])
        self.assertEqual((report.receipts, report.lines, report.discounted_lines), (3, 5, 1))
        self.assertEqual([c.key for c in report.columns], ['date', 'receipt', 'customer', 'sku', 'name', 'description', 'quantity', 'price', 'amount'])

    def test_column_selection(self):
        report = general_sales.build(self.period(), optional_keys={'vendor', 'cost', 'margin'})
        self.assertEqual([c.key for c in report.columns], ['date', 'receipt', 'sku', 'vendor', 'quantity', 'price', 'amount', 'cost', 'margin'])
        report = general_sales.build(self.period(), optional_keys=set())
        self.assertEqual([c.key for c in report.columns], ['date', 'receipt', 'sku', 'quantity', 'price', 'amount'])

    def test_grouping(self):
        report = general_sales.build(self.period(), group_by='department')
        self.assertEqual([g.label for g in report.groups], ['ACCESSORIES', 'Dining Room'])
        self.assertEqual(report.groups[1].totals['quantity'], Decimal('2'))
        self.assertIn('by Department', report.subtitle)
        report = general_sales.build(self.period(), group_by='vendor')
        self.assertEqual([g.label for g in report.groups], ['DEMO WOODSHOP', '(No vendor)'])

    def test_inventory_only_and_discount_toggle(self):
        report = general_sales.build(self.period(), include_noninventory=False)
        self.assertNotIn('FINISH', [r['sku'] for r in report.groups[0].rows])
        self.assertEqual(report.lines, 4)
        self.assertIn('inventory items only', report.subtitle)
        report = general_sales.build(self.period(), highlight_discounts=False)
        self.assertEqual(report.discounted_lines, 0)
        self.assertFalse(any(r['discounted'] for r in report.groups[0].rows))

    def test_exports(self):
        report = general_sales.build(self.period(), optional_keys={'customer', 'vendor', 'cost', 'margin'}, group_by='vendor')
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(tabular_exports.xlsx_bytes(report))).active
        cells = [[c.value for c in row] for row in ws.iter_rows()]
        self.assertEqual(cells[5], ['Date', 'Receipt #', 'Customer', 'Item #', 'Vendor', 'Qty', 'Price', 'Ext Price', 'Ext Cost', 'Margin'])
        grand = [r for r in cells if r and r[0] == 'Grand total'][0]
        self.assertEqual(Decimal(str(grand[7])), report.totals['amount'].normalize())
        self.assertEqual(Decimal(str(grand[9])), report.totals['margin'].normalize())
        self.assertTrue(any(r and r[0] == 'Total DEMO WOODSHOP' for r in cells))
        discounted = [row for row in ws.iter_rows() if row[3].value == 'BW-CHAIR' and row[1].value == '2'][0]
        self.assertEqual(discounted[3].font.color.rgb[-6:], tabular_exports.RED)
        self.assertTrue(tabular_exports.pdf_bytes(report).startswith(b'%PDF'))
        self.assertTrue(tabular_exports.pdf_bytes(general_sales.build(self.period(), optional_keys=set())).startswith(b'%PDF'))
        empty = general_sales.build(ny_periods.resolve('month', '2020-01', date(2026, 9, 30)))
        self.assertTrue(tabular_exports.pdf_bytes(empty).startswith(b'%PDF'))


class GeneralSalesViewTests(AdminModeClientTests):
    @classmethod
    def setUpTestData(cls):
        dept = Department.objects.create(name='Dining Room', code='DIN')
        vendor = Vendor.objects.create(company='DEMO WOODSHOP', code='BW')
        cls.item = Item.objects.create(sku='BW-1', name='Chair', description='Chair', price=Decimal('5'), primary_vendor=vendor, department=dept)
        line(sale(1, (2026, 9, 5)), 1, cls.item, '3', '5', cost='2', original='6')
        cls.brother = Printer.objects.create(name='Test Brother', kind='letter', host='192.0.2.30', port=631)

    def test_defaults_and_columns(self):
        r = self.client.get('/reports/general-sales/')
        self.assertIsNone(r.context['report'])
        self.assertEqual(r.context['chosen'], general_sales.DEFAULT_OPTIONAL)
        r = self.client.get('/reports/general-sales/', {'period': 'month', 'month': '2026-09', 'run': '1', 'cols': ['vendor', 'margin'], 'submitted': '1', 'discounts': ['on', 'off']})
        self.assertContains(r, '<th>Vendor</th>')
        self.assertContains(r, 'Margin</th>')
        self.assertNotContains(r, '<th>Customer</th>')
        self.assertContains(r, 'DEMO WOODSHOP')
        self.assertContains(r, 'class="discounted"')
        self.assertContains(r, 'Sold at Discounted Price')
        self.assertContains(r, '$15.00')
        self.assertContains(r, '$9.00')   # margin 15 - 6
        # unticked discount box: only the hidden 'off' arrives
        r = self.client.get('/reports/general-sales/', {'period': 'month', 'month': '2026-09', 'run': '1', 'submitted': '1', 'discounts': 'off'})
        self.assertNotContains(r, 'class="discounted"')
        self.assertNotContains(r, 'Sold at Discounted Price')
        r = self.client.get('/reports/general-sales/', {'period': 'month', 'month': '2026-09', 'run': '1', 'group': 'department', 'submitted': '1'})
        self.assertContains(r, 'Total Dining Room')

    def test_exports_and_print(self):
        params = {'period': 'month', 'month': '2026-09', 'run': '1'}
        for fmt, magic in [('xlsx', b'PK'), ('pdf', b'%PDF')]:
            r = self.client.get('/reports/general-sales/', {**params, 'format': fmt})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.content.startswith(magic))
            self.assertIn('General-Sales-2026-09-01-2026-09-30.' + fmt, r['Content-Disposition'])
        self.assertEqual(self.client.get('/reports/general-sales/', {**params, 'format': 'csv'}).status_code, 404)
        from sales.test_printing import FakeIPP, ipp_reply
        sent = {}

        def urlopen(request, timeout):
            sent['body'] = request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            r = self.client.post('/reports/general-sales/print/', {'period': 'month', 'month': '2026-09', 'cols': ['vendor'], 'submitted': '1', 'printer': self.brother.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn(b'RaS2', sent['body'])
        self.assertEqual(self.client.post('/reports/general-sales/print/', {'period': 'month', 'month': '2026-09', 'printer': ''}).status_code, 400)

    def test_index_home_and_admin_mode(self):
        self.assertContains(self.client.get('/reports/'), '/reports/general-sales/')
        self.assertContains(self.client.get('/'), '/reports/general-sales/')
        sign_in_register(self.client)   # the shared register login is not an admin
        self.assertRedirects(self.client.get('/reports/general-sales/'), '/?admin=off', fetch_redirect_response=False)
