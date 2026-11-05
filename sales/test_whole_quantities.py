# SPDX-License-Identifier: AGPL-3.0-or-later
"""quantities are whole numbers everywhere — the register, sales orders and pickups reject fractions, and
quantities display without '.000'."""
import json
import uuid
from decimal import Decimal

from django.test import TestCase

from customers.models import Customer
from .models import Item, Line, quantity_text
from .services import save_document, fulfill_order, WorkflowError
from .templatetags.quantity_tags import qty
from .tests import payload
from .views import document_data


class QuantityTextTests(TestCase):
    def test_whole_values_have_no_decimals_and_imported_fractions_keep_theirs(self):
        self.assertEqual([quantity_text(v) for v in [Decimal('5.000'), Decimal('0.000'), 5, Decimal('-2.000'), Decimal('1234.000')]],
                         ['5', '0', '5', '-2', '1234'])
        # The three QB typo lines (a dollar amount keyed into Qty) stay as imported, trailing zeros dropped.
        self.assertEqual([quantity_text(v) for v in [Decimal('60.770'), Decimal('1794.950'), Decimal('67.750')]], ['60.77', '1794.95', '67.75'])
        self.assertEqual(quantity_text(None), '')
        self.assertEqual([qty(v) for v in [Decimal('3.000'), '2.000', 'Unverified', None, '']], ['3', '2', 'Unverified', '', ''])

    def test_tax_rate_shows_without_trailing_zeros_everywhere(self):
        from sales.models import rate_text
        self.assertEqual([rate_text(v) for v in [Decimal('7.000'), Decimal('8.875'), Decimal('0.000')]], ['7', '8.875', '0'])
        item = Item.objects.create(name='RATE TEST', sku='RATE1', price=Decimal('10.00'), tax_rate=Decimal('7.000'), on_hand=Decimal('1.000'))
        page = self.client.get('/pos/sale/new/').content.decode()
        self.assertNotIn('7.000', page)
        lookup = self.client.get('/pos/lookup/items/', {'q': 'RATE TEST'}).json()['results']
        self.assertEqual([r['tax_rate'] for r in lookup if r['id'] == item.pk], ['7'])
        picker = self.client.get('/purchasing/item-picker/', {'q': 'RATE TEST'}).json()['results'][0]
        self.assertEqual(picker['tax_rate'], '7')
        editor = self.client.get(f'/purchasing/items/{item.pk}/').content.decode()
        self.assertIn('name="tax_rate" value="7"', editor)
        self.assertNotIn('7.000', editor)
        listing = self.client.get('/purchasing/items/').content.decode()
        self.assertNotIn('7.000', listing)


class WholeQuantityTests(TestCase):
    def setUp(self):
        self.item = Item.objects.create(sku='CHAIR', name='Side chair', description='Side chair', price='99.95', on_hand='3', tax_rate='7.000')
        self.customer = Customer.objects.create(source_id='c1', full_name='Buyer')

    def test_register_and_order_reject_fractional_quantities(self):
        for kind in ['sale', 'order']:
            for bad in ['2.5', '0.5', '1.001', '0']:
                data = payload(self.item, action='hold' if kind == 'sale' else 'save')
                data['lines'][0]['quantity'] = bad
                with self.subTest(kind=kind, bad=bad), self.assertRaisesMessage(WorkflowError, 'Quantity must be a whole number (at least 1).'):
                    save_document(data, kind, data['token'])
        self.assertEqual(Line.objects.count(), 0)

    def test_register_save_endpoint_rejects_fraction_with_plain_message(self):
        data = payload(self.item)
        data['lines'][0]['quantity'] = '1.5'
        response = self.client.post('/pos/sale/new/', json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Quantity must be a whole number (at least 1).')
        self.assertEqual(Line.objects.count(), 0)

    def test_whole_quantities_save_and_show_without_decimals(self):
        data = payload(self.item)
        data['lines'][0]['quantity'] = '2.000'  # a zero fraction still counts as whole (number inputs may post it)
        doc = save_document(data, 'sale', data['token'])
        line = doc.lines.get()
        self.assertEqual(line.quantity, Decimal('2'))
        rendered = document_data(doc)['lines'][0]
        self.assertEqual((rendered['quantity'], rendered['on_hand']), ('2', '3'))  # the register inputs show 2, not 2.000
        details = self.client.get(f'/pos/held/{doc.token}/details/')
        self.assertContains(details, '<td class="number">2</td>')
        self.assertNotContains(details, '.000')
        listing = self.client.get('/pos/held/')
        self.assertContains(listing, '<td class="number">2</td>')
        self.assertNotContains(listing, '2.000')

    def test_register_page_quantity_inputs_step_by_one(self):
        page = self.client.get('/pos/sale/new/').content.decode()
        self.assertIn('<input name="quantity" type="number" min="1" max="99999999" step="1" value="1" required>', page)  # misc item dialog
        self.assertIn("['quantity','1']", page)  # line inputs (editor-script)
        self.assertIn("input.min=field==='quantity'?'1':'0'", page)
        lookup = self.client.get('/pos/lookup/items/', {'q': 'CHAIR'}).json()['results'][0]
        self.assertEqual(lookup['on_hand'], '3')

    def test_pickup_rejects_fraction_and_shows_whole_quantities(self):
        data = payload(self.item, action='save')
        data['lines'][0]['quantity'] = '2'
        order = save_document(data, 'order', data['token'])
        line = order.lines.get()
        with self.assertRaisesMessage(WorkflowError, 'Pickup quantity must be a whole number.'):
            fulfill_order(order.pk, dict(version=order.version, token=str(uuid.uuid4()), **{f'qty_{line.pk}': '0.5'}))
        page = self.client.get(f'/pos/order/{order.token}/')
        self.assertContains(page, 'type="number" min="0" max="99999999" step="1" value="0"')
        self.assertContains(page, '<td>2</td><td>0</td>')  # Ordered / Already picked up
        self.assertNotContains(page, '<td>2.000</td>')
        self.assertContains(page, 'data-ordered="2" data-fulfilled="0"')
        from .services import save_order_payment
        save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount='250.00', method='cash', reference='', confirm_overpayment='1'))
        order.refresh_from_db()
        receipt = fulfill_order(order.pk, dict(version=order.version, token=str(uuid.uuid4()), **{f'qty_{line.pk}': '1'}))
        self.assertEqual(receipt.lines.get().quantity, Decimal('1'))
