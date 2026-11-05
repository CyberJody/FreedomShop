# SPDX-License-Identifier: AGPL-3.0-or-later
"""the Order List's ticked orders don't outlive making the PO, or a later visit to the list."""
import uuid
from decimal import Decimal

from django.test import TestCase

from customers.models import Customer
from sales.models import Item, Document, Line
from .models import Vendor, PurchaseDocument


class OrderSelectionClearedTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='R78', company='R78 Vendor')
        self.item = Item.objects.create(sku='R78-ITEM', description='Test item', price=50, on_hand=0, average_cost=10,
                                        order_cost=20, primary_vendor=self.vendor, tax_rate=7)
        customer = Customer.objects.create(source_id='r78', full_name='R78 Customer')
        self.orders = []
        for _ in range(2):
            order = Document.objects.create(kind='order', status='open', customer=customer, customer_name='R78 Customer')
            Line.objects.create(document=order, position=0, item=self.item, description='Test item', quantity=1, price=50)
            self.orders.append(order)

    def test_order_list_restores_ticks_only_while_staying_on_the_list(self):
        page = self.client.get('/pos/orders/').content.decode()
        self.assertIn("if(nav==='reload'||nav==='back_forward'||fromList)", page)
        self.assertIn("sessionStorage.removeItem('freedomshop-selected-orders')", page)

    def test_new_po_from_selected_orders_remembers_them(self):
        query = '&'.join(f'order={o.token}' for o in self.orders)
        page = self.client.get(f'/purchasing/po/new/?{query}').content.decode()
        expected = ','.join(f'"{o.token}"' for o in self.orders)
        self.assertIn(f"sessionStorage.setItem('freedomshop-po-from-orders',JSON.stringify([{expected}]))", page)

    def test_saved_po_drops_its_orders_from_the_selection(self):
        query = '&'.join(f'order={o.token}' for o in self.orders)
        lines = {f'lines-{i}-{k}': v for i, o in enumerate(self.orders) for k, v in
                 dict(item=self.item.pk, quantity='1', cost='20', sales_line=o.lines.get().pk).items()}
        data = {'token': str(uuid.uuid4()), 'version': '0', 'vendor': self.vendor.pk, 'status': 'open',
                'lines-TOTAL_FORMS': '2', 'lines-INITIAL_FORMS': '0', 'action': 'save', **lines}
        response = self.client.post(f'/purchasing/po/new/?{query}', data)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        page = self.client.get(response.url).content.decode()
        self.assertIn('id="po-saved-orders"', page)
        for order in self.orders:
            self.assertIn(f'"{order.token}",', page)
        self.assertIn("sessionStorage.removeItem('freedomshop-po-from-orders')", page)
        self.assertEqual(PurchaseDocument.objects.get().lines.count(), 2)

    def test_single_order_generate_po_also_clears_it(self):
        order = self.orders[0]
        data = {'token': str(uuid.uuid4()), 'version': '0', 'vendor': self.vendor.pk, 'status': 'open',
                'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'action': 'save', 'lines-0-item': self.item.pk,
                'lines-0-quantity': '1', 'lines-0-cost': '20', 'lines-0-sales_line': order.lines.get().pk}
        response = self.client.post(f'/purchasing/po/new/?order={order.token}', data)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.url).content.decode()
        self.assertIn(f'"{order.token}",', page)
        self.assertIn("sessionStorage.getItem('freedomshop-selected-orders')", page)
