# SPDX-License-Identifier: AGPL-3.0-or-later
"""whole-number quantities on purchase orders, receiving vouchers and the Item editor (on hand, reorder point);
counts display without '.000' in the purchasing lists, editors, item list and item details."""
import re
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from sales.models import Item
from .forms import WholeNumberField
from .models import Vendor, PurchaseDocument


class WholeQuantityFormTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='TEST', company='Test Vendor')
        self.item = Item.objects.create(sku='TEST-ITEM', name='Test item', description='Test item', price=50, on_hand=2, average_cost=10, order_cost=20, primary_vendor=self.vendor, tax_rate=7)

    def po_data(self, quantity, **extra):
        data = {'token': str(uuid.uuid4()), 'version': '0', 'vendor': self.vendor.pk, 'status': 'open', 'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0',
                'lines-0-item': self.item.pk, 'lines-0-quantity': quantity, 'lines-0-cost': '20', 'action': 'save'}
        data.update(extra)
        return data

    def test_whole_number_field_shows_stored_decimals_without_zeros(self):
        field = WholeNumberField()
        self.assertEqual([field.prepare_value(v) for v in [Decimal('3.000'), Decimal('0.000'), Decimal('60.770'), 4, None]], [3, 0, '60.77', 4, None])
        self.assertEqual(field.widget_attrs(field.widget).get('step'), 1)
        self.assertEqual(field.clean('3.000'), 3)
        with self.assertRaisesMessage(ValidationError, 'Enter a whole number.'):
            field.clean('2.5')

    def test_po_save_rejects_fractional_quantity(self):
        for bad in ['1.5', '0.001', '2.25']:
            with self.subTest(bad=bad):
                response = self.client.post('/purchasing/po/new/', self.po_data(bad))
                self.assertContains(response, 'Quantity must be a whole number.', status_code=400)
        self.assertEqual(PurchaseDocument.objects.count(), 0)
        response = self.client.post('/purchasing/po/new/', self.po_data('3.000'))  # zero fraction = whole
        self.assertEqual(response.status_code, 302)
        po = PurchaseDocument.objects.get()
        self.assertEqual(po.lines.get().quantity, Decimal('3'))
        editor = self.client.get(f'/purchasing/po/{po.token}/').content.decode()
        self.assertIn('name="lines-0-quantity" value="3"', editor)  # not 3.000
        self.assertIn('step="1"', editor)
        self.assertNotIn('step="0.001"', editor)
        self.assertNotIn('>3.000<', editor)
        listing = self.client.get('/purchasing/orders/').content.decode()
        self.assertIn('<td>3</td><td>3</td><td>0</td>', listing)  # Ordered / Due / Received
        self.assertNotIn('3.000', listing)

    def test_voucher_post_rejects_fractional_quantity(self):
        self.assertEqual(self.client.post('/purchasing/po/new/', self.po_data('4')).status_code, 302)
        po = PurchaseDocument.objects.get()
        receive = self.client.get(f'/purchasing/voucher/new/?po={po.token}').content.decode()
        self.assertIn('name="lines-0-quantity" value="4"', receive)  # Receive All Due prefill without.000
        data = {'token': str(uuid.uuid4()), 'version': '0', 'vendor': self.vendor.pk, 'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0',
                'lines-0-item': self.item.pk, 'lines-0-quantity': '2.5', 'lines-0-cost': '20', 'lines-0-source_line': po.lines.get().pk, 'action': 'post'}
        response = self.client.post(f'/purchasing/voucher/new/?po={po.token}', data)
        self.assertContains(response, 'Quantity must be a whole number.', status_code=400)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand, Decimal('2'))  # nothing received
        data['lines-0-quantity'] = '2'
        self.assertEqual(self.client.post(f'/purchasing/voucher/new/?po={po.token}', data).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand, Decimal('4'))

    def test_item_editor_on_hand_and_reorder_point_are_whole_numbers(self):
        page = self.client.get(f'/purchasing/items/{self.item.pk}/').content.decode()
        on_hand = re.search(r'<input[^>]*name="on_hand"[^>]*>', page).group(0)
        self.assertIn('value="2"', on_hand)
        self.assertIn('step="1"', on_hand)
        reorder = re.search(r'<input[^>]*name="reorder_point"[^>]*>', page).group(0)
        self.assertIn('step="1"', reorder)
        self.assertNotIn('value="2.000"', page)
        self.assertNotIn('step="0.001"', on_hand + reorder)  # shipping weight/dimensions stay 3-place (measurements, not counts)
        version = re.search(r'name="version" value="(\d+)"', page).group(1)
        data = {'name': 'Test item', 'item_type': 'inventory', 'price': '50', 'on_hand': '2.5', 'version': version, 'tax_category': 'Taxable Sales'}
        response = self.client.post(f'/purchasing/items/{self.item.pk}/', data)
        self.assertContains(response, 'Enter a whole number.', status_code=400)
        data.update(on_hand='7', reorder_point='1.5')
        self.assertContains(self.client.post(f'/purchasing/items/{self.item.pk}/', data), 'Enter a whole number.', status_code=400)
        data.update(reorder_point='3')
        response = self.client.post(f'/purchasing/items/{self.item.pk}/', data)
        self.assertEqual(response.status_code, 302, str(response.context['form'].errors) if response.context else '')
        self.item.refresh_from_db()
        self.assertEqual((self.item.on_hand, self.item.reorder_point), (Decimal('7'), Decimal('3')))
        self.assertContains(self.client.get(f'/purchasing/items/{self.item.pk}/'), 'Quantity: 2 → 7')  # Stock and Cost Changes, no.000

    def test_item_list_rows_picker_and_details_show_whole_counts(self):
        import json
        from django.test import RequestFactory
        from .item_scrolling import item_rows
        from .catalog_quantities import catalog_quantities
        response = item_rows(RequestFactory().get('/', {'offset': '0'}), catalog_quantities(Item.objects.all()), 'inventory')
        cells = json.loads(response.content)['rows'][0]['cells']
        self.assertEqual(cells[2], '2')  # On Hand column
        self.assertEqual(cells[3], '0')  # Held
        self.assertEqual(cells[5], '0')  # On Order
        picker = self.client.get('/purchasing/item-picker/', {'q': 'TEST'}).json()['results'][0]
        self.assertEqual((picker['on_hand'], picker['on_order']), ('2', '0'))
        details = self.client.get(f'/purchasing/items/{self.item.pk}/details/').content.decode()
        self.assertIn('<span class="detail-qty">2</span>', details)  # On Hand Qty
        self.assertNotIn('2.000', details)
        listing = self.client.get('/purchasing/items/').content.decode()  # server-rendered first page of the Item List
        self.assertIn('<td>2</td><td>0</td><td>$50.00</td><td>0</td>', listing)  # On Hand / Price ($ like the loaded rows) / On Order
        self.assertNotIn('2.000', listing)
        Item.objects.filter(pk=self.item.pk).update(on_hand=None)
        response = item_rows(RequestFactory().get('/', {'offset': '0'}), catalog_quantities(Item.objects.filter(pk=self.item.pk)), 'inventory')
        self.assertEqual(json.loads(response.content)['rows'][0]['cells'][2], 'Unverified')  # virtual-scroll row keeps the label (review finding 1)
        self.assertIn('<span class="detail-qty">Unverified</span>', self.client.get(f'/purchasing/items/{self.item.pk}/details/').content.decode())
        self.assertIn('<dt>On hand</dt><dd>Unverified</dd>', self.client.get(f'/purchasing/items/{self.item.pk}/').content.decode())
