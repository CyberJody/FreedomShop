# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a new item starts with 0 on hand, so it can be received right away."""
import uuid
from decimal import Decimal as D
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from customers.models import Customer
from sales.models import Document, InventoryAdjustment, Item, Line, StockMovement
from sales.services import WorkflowError
from .models import ReceivingMovement, Vendor
from .services import save_purchase


def header(vendor):
    return dict(token=uuid.uuid4(), version=0, vendor=vendor, status='open', notes='', terms='', ship_date=None,
                payment_due=None, invoice_number='')


def rows(item, quantity, cost, **kw):
    return [dict(item=item, quantity=D(quantity), cost=D(cost), **kw)]


class NewItemStockTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='R63V', company='R63 Vendor')
        # Blank On-Hand and Avg. Unit Cost, exactly as the tester left them.
        self.data = dict(name='R63-CHAIR', item_type='inventory', description='Test chair', price='100', on_hand='',
                         average_cost='', order_cost='', tax_category='Taxable Sales', tax_rate='7',
                         primary_vendor=self.vendor.pk, active='on', version='1')

    def add_item(self, **changes):
        response = self.client.post('/purchasing/items/new/', {**self.data, **changes})
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['form'].errors)
        return Item.objects.latest('pk')

    def receive(self, item, quantity, cost, **po_kw):
        po = save_purchase(header(self.vendor), rows(item, quantity, cost, **po_kw.pop('line', {})), 'po', 'save', **po_kw)
        save_purchase(header(self.vendor), rows(item, quantity, cost, source_line=po.lines.get().pk), 'voucher', 'post', po)
        item.refresh_from_db()
        return po

    def test_new_item_form_shows_zero_and_blank_on_hand_saves_zero(self):
        self.assertContains(self.client.get('/purchasing/items/new/'), 'name="on_hand" value="0"')
        item = self.add_item()
        self.assertEqual(item.on_hand, 0)
        self.assertIsNone(item.average_cost)
        self.assertIsNone(item.source_id)
        # The starting 0 is not a stock change: no adjustment is recorded for it.
        self.assertFalse(InventoryAdjustment.objects.filter(item=item).exists())

    def test_new_item_with_a_typed_count_still_records_the_adjustment(self):
        item = self.add_item(on_hand='4', average_cost='25', adjustment_note='Opening count')
        self.assertEqual((item.on_hand, item.average_cost), (4, D('25.00')))
        adjustment = InventoryAdjustment.objects.get(item=item)
        self.assertEqual((adjustment.old_quantity, adjustment.new_quantity, adjustment.new_cost), (None, 4, D('25.00')))

    def test_new_item_is_received_at_cost(self):
        item = self.add_item()
        self.receive(item, '3', '42.50')
        self.assertEqual(item.on_hand, 3)
        self.assertEqual(item.average_cost, D('42.50'))
        self.assertEqual(item.order_cost, D('42.50'))
        self.receive(item, '1', '50.50')  # weighted: (3 × 42.50 + 50.50) / 4 = 44.50
        self.assertEqual((item.on_hand, item.average_cost), (4, D('44.50')))

    def test_special_order_item_from_a_sales_order_can_be_received(self):
        # The sales order's I Want to… > Add New Item opens the same Add Item screen.
        item = self.add_item(name='R63-SOFA SOLD', description='Special-order sofa')
        customer = Customer.objects.create(source_id='r63', full_name='R63 Customer')
        order = Document.objects.create(kind='order', status='open', customer=customer, customer_name='R63 Customer')
        line = Line.objects.create(document=order, position=0, item=item, description=item.description, quantity=1, price=1500)
        po = self.receive(item, '1', '800', source_order=order, line=dict(sales_line=line.pk))
        self.assertEqual(po.source_order_id, order.pk)
        self.assertEqual((item.on_hand, item.average_cost), (1, D('800.00')))
        self.assertEqual(ReceivingMovement.objects.filter(item=item).count(), 1)

    def test_copy_item_is_unchanged(self):
        source = self.add_item(on_hand='5', average_cost='30', order_cost='30')
        page = self.client.get('/purchasing/items/new/', {'copy': source.pk})
        self.assertContains(page, 'name="on_hand" value="0"')
        self.assertContains(page, 'name="average_cost" value="30.00"')
        copy = self.add_item(name='R63-CHAIR COPY', on_hand='0', average_cost='30.00', order_cost='30.00')
        self.assertEqual((copy.on_hand, copy.average_cost), (0, D('30.00')))
        # As before, a copy carrying a cost records its opening adjustment.
        self.assertTrue(InventoryAdjustment.objects.filter(item=copy, new_quantity=0, new_cost=D('30.00')).exists())

    def test_existing_imported_item_with_unknown_count_keeps_it(self):
        imported = Item.objects.create(source_id='QB-17044', sku='17044', name='OLD', description='Old', price=10,
                                       on_hand=None, tax_rate=7, primary_vendor=self.vendor, version=1)
        data = {**self.data, 'name': 'OLD', 'version': '1'}
        self.assertEqual(self.client.post(f'/purchasing/items/{imported.pk}/', data).status_code, 302)
        imported.refresh_from_db()
        self.assertIsNone(imported.on_hand)  # still Unverified
        with self.assertRaisesMessage(WorkflowError, 'Item 17044 has no stock count yet. Ask a manager to set its On-Hand Qty in the Item List.'):
            save_purchase(header(self.vendor), rows(imported, '1', '5'), 'voucher', 'post')


class FixNewItemStockCommandTests(TestCase):
    def setUp(self):
        mk = lambda sku, **kw: Item.objects.create(sku=sku, name=sku, description=sku, price=10, tax_rate=7, **{'on_hand': None, **kw})
        self.stuck = mk('9001')
        self.stuck_blank_source = mk('9002', source_id='')
        self.imported = mk('9003', source_id='QB-9003')
        self.counted = mk('9004', on_hand=2)
        self.moved = mk('9005')
        receipt = Document.objects.create(kind='sale', status='completed')
        StockMovement.objects.create(document=receipt, item=self.moved, quantity=-1)
        self.adjusted = mk('9006')
        InventoryAdjustment.objects.create(item=self.adjusted, old_quantity=3, new_quantity=None)
        self.adjusted_to_count = mk('9007')
        InventoryAdjustment.objects.create(item=self.adjusted_to_count, old_quantity=None, new_quantity=1)

    def run_command(self, *args):
        out = StringIO()
        call_command('fix_new_item_stock', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_lists_and_changes_nothing(self):
        out = self.run_command()
        for sku in ('9001', '9002', '9006'):
            self.assertIn(f'item {sku} ', out)
        for sku in ('9003', '9004', '9005', '9007'):
            self.assertNotIn(f'item {sku} ', out)
        self.assertIn('3 app-made item(s) would be set to On-Hand 0 (dry run', out)
        self.assertIn('2 app-made item(s) with an unknown count skipped', out)
        self.assertEqual(Item.objects.filter(on_hand__isnull=True).count(), 6)

    def test_commit_sets_zero_once(self):
        self.assertIn('3 app-made item(s) set to On-Hand 0.', self.run_command('--commit'))
        for item in (self.stuck, self.stuck_blank_source, self.adjusted):
            item.refresh_from_db()
            self.assertEqual((item.on_hand, item.version), (0, 2))
        for item in (self.imported, self.moved, self.adjusted_to_count):
            item.refresh_from_db()
            self.assertIsNone(item.on_hand)
        self.assertIn('0 app-made item(s) set to', self.run_command('--commit'))
        # Now the repaired item can be received.
        vendor = Vendor.objects.create(code='R63C', company='R63 C')
        save_purchase(header(vendor), rows(self.stuck, '2', '15'), 'voucher', 'post')
        self.stuck.refresh_from_db()
        self.assertEqual((self.stuck.on_hand, self.stuck.average_cost), (2, D('15.00')))
