# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reorder Reminders (/ readiness N8): QB rule on hand + on order - committed <= reorder point."""
import uuid
from decimal import Decimal

from django.test import TestCase, Client

from sales.models import Item, Document, Line
from .models import Vendor, ReorderIgnore
from .services import save_purchase
from . import reorder


def po_header(vendor):
    return dict(token=uuid.uuid4(), version=0, vendor=vendor, status='open', notes='', terms='', ship_date=None,
                payment_due=None, invoice_number='')


class ReorderRuleTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='DR/', company='Test Vendor')
        self.item = self.make('CHAIR', on_hand=3, point=4)

    def make(self, sku, on_hand, point, **kw):
        return Item.objects.create(sku=sku, name=sku, description=f'{sku} test', price=10, on_hand=on_hand, reorder_point=point,
                                   primary_vendor=self.vendor, tax_rate=7, **kw)

    def listed(self, rule=reorder.DEFAULT_RULE):
        return [row['item'].sku for row in reorder.reminders(rule)[0]]

    def order(self, item, quantity, status='open'):
        doc = Document.objects.create(kind='order', status=status)
        return Line.objects.create(document=doc, item=item, position=0, description='TEST', quantity=quantity, price=10, tax_rate=7)

    def test_on_hand_at_or_below_point(self):
        self.make('EXACT', on_hand=4, point=4)
        self.make('ABOVE', on_hand=5, point=4)
        self.assertEqual(sorted(self.listed()), ['CHAIR', 'EXACT'])

    def test_point_zero_or_blank_never_reminds(self):
        self.make('ZERO', on_hand=0, point=0)
        self.make('BLANK', on_hand=0, point=None)
        self.make('INACTIVE', on_hand=0, point=4, active=False)
        self.make('NOORDER', on_hand=0, point=4, unorderable=True)
        self.assertEqual(self.listed(), ['CHAIR'])

    def test_open_po_quantity_counts(self):
        save_purchase(po_header(self.vendor), [dict(item=self.item, quantity=Decimal('2'), cost=Decimal('5'))], 'po', 'save')
        self.assertEqual(self.listed(), [])  # 3 on hand + 2 on order = 5 > 4
        row = next(r for r in reorder.evaluate() if r['item'] == self.item)
        self.assertEqual((row['on_order'], row['projected']), (Decimal('2'), Decimal('5')))
        self.make('QB-PO', on_hand=1, point=4, imported_on_order=4)  # QB's item total from the backup is no longer read
        self.assertIn('QB-PO', self.listed())                        # (on order comes from open PO lines dated 2026 or later)

    def test_committed_on_open_customer_orders_counts_by_default_only(self):
        self.make('SPARE', on_hand=6, point=4)
        spare = Item.objects.get(sku='SPARE')
        self.order(spare, 3)  # 6 - 3 = 3 <= 4
        self.order(spare, 5, status='cancelled')  # cancelled orders don't commit stock
        self.assertEqual(sorted(self.listed()), ['CHAIR', 'SPARE'])
        self.assertEqual(self.listed('on_hand'), ['CHAIR'])
        row = next(r for r in reorder.evaluate() if r['item'] == spare)
        self.assertEqual((row['committed'], row['available']), (Decimal('3'), Decimal('3')))

    def test_picked_up_quantity_is_no_longer_committed(self):
        spare = self.make('SPARE', on_hand=6, point=4)
        line = self.order(spare, 3)
        sale = Document.objects.create(kind='sale', status='completed')
        Line.objects.create(document=sale, item=spare, position=0, description='TEST', quantity=3, price=10, tax_rate=7, source_order_line=line)
        self.assertEqual(self.listed(), ['CHAIR'])

    def test_ignore_hides_until_restocked_then_clears(self):
        reorder.set_ignored(self.item, True)
        listed, ignored = reorder.reminders()
        self.assertEqual((listed, [r['item'] for r in ignored]), ([], [self.item]))
        self.assertEqual(reorder.reminder_count(), 0)
        self.assertEqual(reorder.clear_restocked(), 0)  # still short: the ignore stays
        Item.objects.filter(pk=self.item.pk).update(on_hand=9)
        self.assertEqual(reorder.clear_restocked(), 1)
        self.assertFalse(ReorderIgnore.objects.exists())
        Item.objects.filter(pk=self.item.pk).update(on_hand=1)
        self.assertEqual(self.listed(), ['CHAIR'])  # short again: back on the list

    def test_screen_home_message_ignore_and_create_po_link(self):
        client = Client()
        page = client.get('/purchasing/reorder/').content.decode()
        self.assertIn('Reorder Reminders', page)
        self.assertIn('1 item at or below its reorder point', page)
        self.assertIn(f'/purchasing/po/new/?vendor={self.vendor.pk}&amp;item={self.item.pk}', page)
        home = client.get('/').content.decode()
        self.assertIn('You have inventory reminders', home)
        self.assertIn('1 item at or below reorder point', home)
        response = client.post(f'/purchasing/reorder/{self.item.pk}/ignore/', {'ignore': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ReorderIgnore.objects.filter(item=self.item).exists())
        page = client.get('/purchasing/reorder/').content.decode()
        self.assertIn('Ignored for now (1)', page)
        self.assertNotIn('You have inventory reminders', client.get('/').content.decode())
        client.post(f'/purchasing/reorder/{self.item.pk}/ignore/', {'ignore': '0'})
        self.assertFalse(ReorderIgnore.objects.exists())
        po = client.get(f'/purchasing/po/new/?vendor={self.vendor.pk}&item={self.item.pk}').content.decode()
        self.assertIn('CHAIR', po)
        self.assertEqual(client.get('/purchasing/reorder/?rule=on_hand').status_code, 200)
        self.make('PLENTY', on_hand=9, point=2)
        self.assertNotIn('PLENTY', client.get('/purchasing/reorder/').content.decode())
        page = client.get('/purchasing/reorder/?show=all').content.decode()
        self.assertIn('PLENTY', page)
        self.assertIn('Not short', page)
        self.assertIn('(1 of 2 short)', page)
        response = client.post(f'/purchasing/reorder/{self.item.pk}/ignore/', {'ignore': '1', 'show': 'all', 'rule': 'on_hand'})
        self.assertEqual(response['Location'], f'/purchasing/reorder/?rule=on_hand&show=all#item-{self.item.pk}')
        self.assertEqual(client.get(f'/purchasing/reorder/{self.item.pk}/ignore/').status_code, 405)
