# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inventory On Hand report: department grouping, whole-cent valuation, exports, direct print."""
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.test import TestCase

from purchasing.models import Department, Vendor
from sales.models import Item, Printer

from . import inventory_exports, inventory_on_hand
from .tests import AdminModeClientTests


def item(sku, dept, qty, cost, vendor=None, **extra):
    return Item.objects.create(sku=sku, name=sku, description=f'Desc {sku}', price=Decimal('10'), on_hand=Decimal(qty),
                               average_cost=Decimal(cost), department=dept, primary_vendor=vendor, **extra)


class InventoryOnHandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dining = Department.objects.create(name='Dining Room', code='DIN')
        cls.acc = Department.objects.create(name='ACCESSORIES', code='ACC')
        cls.vendor = Vendor.objects.create(company='DEMO WOODSHOP', code='BW')
        item('D-1', cls.dining, '2', '100.00', cls.vendor)
        item('D-2', cls.dining, '1', '33.333333')        # fractional-cent cost -> 33.33
        item('D-3', cls.dining, '3', '0.999')             # -> 1.00 x 3 = 3.00
        item('A-1', cls.acc, '-1', '50.00')               # negative on hand
        item('A-2', cls.acc, '0', '20.00')                # zero: hidden by default
        item('N-1', None, '4', '5.00')                    # no department
        item('X-1', cls.acc, '9', '1.00', item_type='noninventory')   # not inventory
        item('R-1', cls.acc, '0', '1.00', active=False)   # in the Items Trash, stock zeroed: hidden even with include_zero
        item('R-2', cls.acc, '2', '30.00', active=False)  # in the Items Trash but received/returned while there: shown, labelled

    def test_groups_totals_and_whole_cents(self):
        report = inventory_on_hand.build()
        self.assertEqual([g.department for g in report.groups], ['ACCESSORIES', 'Dining Room', '(No department)'])
        dining = report.groups[1]
        self.assertEqual([(r.sku, r.cost, r.value) for r in dining.rows],
                         [('D-1', Decimal('100.00'), Decimal('200.00')), ('D-2', Decimal('33.33'), Decimal('33.33')), ('D-3', Decimal('1.00'), Decimal('3.00'))])
        self.assertEqual(dining.rows[0].vendor, 'DEMO WOODSHOP')
        self.assertEqual((dining.items, dining.on_hand, dining.value), (3, Decimal('6'), Decimal('236.33')))
        acc = report.groups[0]
        self.assertEqual([(r.sku, r.name, r.on_hand, r.value, r.trashed) for r in acc.rows],
                         [('A-1', 'A-1', Decimal('-1'), Decimal('-50.00'), False), ('R-2', 'R-2 (in Items Trash)', Decimal('2'), Decimal('60.00'), True)])
        self.assertEqual(report.items, 6)
        self.assertEqual(report.on_hand, Decimal('11'))
        self.assertEqual(report.value, Decimal('236.33') - Decimal('50.00') + Decimal('20.00') + Decimal('60.00'))
        self.assertEqual((report.negative_items, report.trashed_items), (1, 1))
        self.assertEqual(report.skipped_zero, 1)
        self.assertEqual(report.subtitle, 'Items with stock on hand')

    def test_include_zero(self):
        report = inventory_on_hand.build(include_zero=True)
        self.assertEqual(report.items, 7)
        self.assertEqual(report.skipped_zero, 0)
        skus = [r.sku for g in report.groups for r in g.rows]
        self.assertIn('A-2', skus)
        self.assertNotIn('R-1', skus)  # trashed with nothing on hand stays out
        self.assertEqual(report.subtitle, 'All inventory items')

    def test_exports(self):
        report = inventory_on_hand.build()
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(inventory_exports.xlsx_bytes(report))).active
        cells = [[c.value for c in row] for row in ws.iter_rows()]
        grand = [r for r in cells if r and r[0] == 'Grand total'][0]
        self.assertEqual(Decimal(str(grand[6])), report.value.normalize())
        self.assertEqual(grand[1], '6 items')
        self.assertTrue(any(r and r[0] == 'D-2' and Decimal(str(r[5])) == Decimal('33.33') for r in cells))
        self.assertTrue(any(r and r[0] == 'R-2' and r[1] == 'R-2 (in Items Trash)' for r in cells))
        self.assertTrue(any(r and str(r[0]).startswith('1 item show a negative') for r in cells))
        self.assertTrue(any(r and str(r[0]).startswith('1 item marked "(in Items Trash)"') for r in cells))
        pdf = inventory_exports.pdf_bytes(report)
        self.assertTrue(pdf.startswith(b'%PDF'))


class InventoryOnHandTrashTests(AdminModeClientTests):
    """review finding 3: stock that reaches an item while it is in the Items Trash (received on an existing PO line,
    restocked by a return) is real inventory and must stay in the valuation, labelled; pickers still hide the item."""

    def setUp(self):
        super().setUp()
        from customers.models import Customer
        self.dept = Department.objects.create(name='Dining Room', code='DIN')
        self.vendor = Vendor.objects.create(company='Vendor', code='V')
        self.customer = Customer.objects.create(source_id='c1', full_name='Pat Buyer')
        self.chair = Item.objects.create(sku='CHAIR', name='Chair', description='Oak chair', price=Decimal('100'), on_hand=Decimal('5'), average_cost=Decimal('40'), order_cost=Decimal('40'), tax_rate=7, department=self.dept, primary_vendor=self.vendor, reorder_point=Decimal('10'))

    def rows(self, include_zero=True):
        return {r.sku: r for g in inventory_on_hand.build(include_zero).groups for r in g.rows}

    def test_received_while_trashed_is_listed_and_labelled_but_stays_out_of_pickers(self):
        import uuid
        from purchasing.services import save_purchase
        from sales import trash
        po = save_purchase(dict(token=uuid.uuid4(), version=0, vendor=self.vendor, status='open', notes='', terms='', ship_date=None, payment_due=None, invoice_number=''), [dict(item=self.chair, quantity=Decimal('2'), cost=Decimal('100'))], 'po', 'save')
        trash.trash([self.chair.pk])
        self.assertEqual(self.rows(), {})  # zeroed on the way in: nothing to value
        save_purchase(dict(token=uuid.uuid4(), version=0, vendor=self.vendor, status='open', notes='', terms='', ship_date=None, payment_due=None, invoice_number=''), [dict(item=self.chair, quantity=Decimal('2'), cost=Decimal('100'), source_line=po.lines.get().pk)], 'voucher', 'post', source_po=po)
        row = self.rows(include_zero=False)['CHAIR']
        self.assertEqual((row.name, row.on_hand, row.cost, row.value, row.trashed), ('Chair (in Items Trash)', Decimal('2'), Decimal('57.14'), Decimal('114.28'), True))
        report = inventory_on_hand.build()
        self.assertEqual((report.items, report.trashed_items, report.value), (1, 1, Decimal('114.28')))
        self.assertContains(self.client.get('/reports/inventory-on-hand/', {'zero': 'stock', 'run': '1'}), '(in Items Trash)')
        for url in ['/pos/lookup/items/?q=Chair', '/purchasing/lookup/items/?q=Chair', '/purchasing/item-picker/?q=Chair']:
            self.assertEqual(self.client.get(url).json()['results'], [], url)
        self.assertNotContains(self.client.get('/purchasing/reorder/'), 'Oak chair')
        trash.restore([self.chair.pk])
        row = self.rows()['CHAIR']
        self.assertEqual((row.name, row.on_hand, row.cost, row.trashed), ('Chair', Decimal('7'), Decimal('57.14'), False))

    def test_returned_while_trashed_is_listed_and_labelled(self):
        import uuid
        from sales import trash
        from sales.services import save_document
        sale = save_document(dict(action='complete', customer_id=self.customer.pk, payment_method='cash', tendered='107', lines=[dict(item_id=self.chair.pk, quantity='1', price='100')]), 'sale', uuid.uuid4())
        trash.trash([self.chair.pk])
        save_document(dict(action='complete', customer_id=self.customer.pk, payment_method='cash', tendered='-107', lines=[dict(returned_from_line=sale.lines.get().pk, quantity='-1', return_reason='other')]), 'sale', uuid.uuid4())
        row = self.rows(include_zero=False)['CHAIR']
        self.assertEqual((row.name, row.on_hand, row.cost, row.value, row.trashed), ('Chair (in Items Trash)', Decimal('1'), Decimal('40.00'), Decimal('40.00'), True))
        trash.restore([self.chair.pk])
        self.assertEqual((self.rows()['CHAIR'].on_hand, self.rows()['CHAIR'].trashed), (Decimal('5'), False))


class InventoryOnHandViewTests(AdminModeClientTests):
    @classmethod
    def setUpTestData(cls):
        dept = Department.objects.create(name='Dining Room', code='DIN')
        item('D-1', dept, '2', '100.00')
        item('D-0', dept, '0', '5.00')
        cls.brother = Printer.objects.create(name='Test Brother', kind='letter', host='192.0.2.30', port=631)

    def test_form_display_and_zero_option(self):
        r = self.client.get('/reports/inventory-on-hand/')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context['report'])
        r = self.client.get('/reports/inventory-on-hand/', {'zero': 'stock', 'run': '1'})
        self.assertContains(r, 'Dining Room')
        self.assertContains(r, '$200.00')
        self.assertNotContains(r, '<td>D-0</td>')
        self.assertContains(r, '1 item with zero on hand not shown')
        r = self.client.get('/reports/inventory-on-hand/', {'zero': 'all', 'run': '1'})
        self.assertContains(r, '<td>D-0</td>')

    def test_exports_and_print(self):
        for fmt, magic in [('xlsx', b'PK'), ('pdf', b'%PDF')]:
            r = self.client.get('/reports/inventory-on-hand/', {'zero': 'stock', 'run': '1', 'format': fmt})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.content.startswith(magic))
            self.assertIn('Inventory-On-Hand-', r['Content-Disposition'])
        self.assertEqual(self.client.get('/reports/inventory-on-hand/', {'run': '1', 'format': 'csv'}).status_code, 404)
        from sales.test_printing import FakeIPP, ipp_reply
        sent = {}

        def urlopen(request, timeout):
            sent['body'] = request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            r = self.client.post('/reports/inventory-on-hand/print/', {'zero': 'stock', 'printer': self.brother.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn(b'RaS2', sent['body'])
        self.assertEqual(self.client.post('/reports/inventory-on-hand/print/', {'printer': ''}).status_code, 400)

    def test_index_and_home(self):
        self.assertContains(self.client.get('/reports/'), '/reports/inventory-on-hand/')
        self.assertContains(self.client.get('/'), '/reports/inventory-on-hand/')

    def test_requires_admin_mode(self):
        self.client.logout()   # nobody signed in
        self.assertRedirects(self.client.get('/reports/inventory-on-hand/'), '/?admin=off', fetch_redirect_response=False)
