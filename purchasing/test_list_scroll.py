# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuous-scroll row batches for the document, vendor and department lists."""
from decimal import Decimal
import re
from django.test import TestCase
from sales.models import Document, Item, Line
from customers.models import Customer
from purchasing.models import Vendor, Department, PurchaseDocument, PurchaseLine
from purchasing.views import linked_orders


class ListScrollTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(full_name='Scroll Customer')
        cls.vendor = Vendor.objects.create(code='SCR', company='Scroll Vendor', phone='555-0100')
        Department.objects.create(code='SD', name='Scroll Dept')
        cls.item = Item.objects.create(sku='SCR-1', name='Scroll item', price='10', on_hand=5)
        cls.orders = [Document.objects.create(kind='order', status='open', customer=cls.customer, customer_name=cls.customer.full_name,
                                              subtotal=10, tax=0, total=10) for _ in range(3)]
        for order in cls.orders:
            Line.objects.create(document=order, item=cls.item, sku='SCR-1', item_name='Scroll item', description='Scroll item',
                                quantity=1, price=10, tax_rate=0, position=1)
        cls.po = PurchaseDocument.objects.create(kind='po', status='open', vendor=cls.vendor, vendor_name=cls.vendor.company,
                                                 source_order=cls.orders[0], total=0)
        PurchaseLine.objects.create(document=cls.po, position=1, item=cls.item, sku='SCR-1', item_name='Scroll item',
                                    description='Scroll item', quantity=1, cost=0, sales_line=cls.orders[1].lines.first())

    def rows(self, path, **params):
        response = self.client.get(path, {'format': 'rows', 'offset': 0, **params})
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        return response.json()

    def test_every_list_serves_rows_matching_its_column_count(self):
        for path, params in [('/pos/orders/', {'status': 'all'}), ('/pos/history/', {'status': 'all'}), ('/pos/held/', {}),
                             ('/purchasing/orders/', {'status': 'all'}), ('/purchasing/history/', {'status': 'all'}),
                             ('/purchasing/held/', {}), ('/purchasing/vendors/', {}), ('/purchasing/departments/', {})]:
            page = self.client.get(path, params)
            config = page.context['list_scroll_config']
            data = self.rows(path, **params)
            self.assertEqual(data['offset'], 0)
            self.assertEqual(data['total'], page.context['page'].paginator.count, path)
            checkbox = 1 if config.get('checkbox') else 0
            for row in data['rows']:
                self.assertEqual(len(row['cells']) + checkbox, config['columns'], path)
                self.assertTrue(row['name'])
            self.assertEqual(len(re.findall(r'<th[ >]', page.content.decode())), config['columns'], path)

    def test_sales_orders_carry_tokens_for_po_selection(self):
        data = self.rows('/pos/orders/', status='all')
        self.assertEqual(data['total'], 3)
        self.assertEqual({row['token'] for row in data['rows']}, {str(o.token) for o in self.orders})
        self.assertEqual(self.client.get('/pos/orders/').context['list_scroll_config']['checkbox'], 'order')
        self.assertEqual(self.client.get('/pos/history/').context['list_scroll_config']['checkbox'], '')

    def test_po_rows_link_customer_orders_from_source_order_and_lines(self):
        data = self.rows('/purchasing/orders/')
        linked = data['rows'][0]['cells'][8]
        texts = [segment['text'] for segment in linked if isinstance(segment, dict)]
        self.assertEqual(set(texts), {f'{self.orders[0].numbered_label} · Scroll Customer', f'{self.orders[1].numbered_label} · Scroll Customer'})  # 'Sales Order N' beside PO numbers
        # The batch resolver must agree with the per-document property.
        prepared = linked_orders([PurchaseDocument.objects.get(pk=self.po.pk)])[0]
        self.assertEqual({o.pk for o in prepared.customer_orders}, {self.orders[0].pk, self.orders[1].pk})
        fresh = PurchaseDocument.objects.get(pk=self.po.pk)
        self.assertEqual({o.pk for o in fresh.customer_orders}, {self.orders[0].pk, self.orders[1].pk})

    def test_invalid_offsets_are_rejected_and_paged_view_still_renders(self):
        for path in ['/pos/history/', '/purchasing/orders/', '/purchasing/vendors/']:
            for offset in ['-1', 'a', '1.5', '9999999999']:
                self.assertEqual(self.client.get(path, {'format': 'rows', 'offset': offset}).status_code, 400, path)
            page = self.client.get(path, {'view': 'pages'})
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.context['list_scroll_config']['paged'])
            self.assertNotIn('view=', page.context['list_scroll_config']['params'])

    def test_vendor_rows_link_to_edit_screen(self):
        data = self.rows('/purchasing/vendors/')
        row = next(r for r in data['rows'] if r['name'] == 'Scroll Vendor')
        self.assertEqual(row['cells'][0][0]['href'], f'/purchasing/vendors/{self.vendor.pk}/')
        self.assertEqual(row['cells'][3], '555-0100')   # Contact is column 3 (blank here)
