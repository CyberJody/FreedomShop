# SPDX-License-Identifier: AGPL-3.0-or-later
"""a short "saved" note after saving a vendor, a customer, or a sales order payment (matching QB)."""
import uuid

from django.test import TestCase

from customers.models import Customer
from purchasing.models import Vendor
from .models import Document, Line, Item, OrderPayment


class VendorSavedNoteTests(TestCase):
    def test_new_vendor_says_added_and_an_edit_says_saved(self):
        response = self.client.post('/purchasing/vendors/new/', {'code': 'R81', 'company': 'R81 Vendor', 'active': 'on'})
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.url).content.decode()
        self.assertIn('<div class="success" role="status">Vendor added.</div>', page)
        self.assertNotIn('Edit Vendor saved.', page)
        vendor = Vendor.objects.get(code='R81')
        response = self.client.post(f'/purchasing/vendors/{vendor.pk}/', {'code': 'R81', 'company': 'R81 Vendor Co', 'active': 'on',
                                                                         'version': vendor.version})
        self.assertEqual(response.status_code, 302)
        self.assertIn('<div class="success" role="status">Vendor saved.</div>', self.client.get(response.url).content.decode())


class CustomerSavedNoteTests(TestCase):
    def test_new_customer_says_added_and_an_edit_says_saved(self):
        from customers.forms import CustomerForm
        blank = Customer(full_name='TEST R81', first_name='TEST', last_name='R81', active=True)
        fields = {f: (getattr(blank, f) if getattr(blank, f) is not None else '') for f in CustomerForm.Meta.fields}
        fields = {k: ('on' if v is True else v) for k, v in fields.items() if v is not False}
        response = self.client.post('/customers/new/', fields)
        self.assertEqual(response.status_code, 302, response.content[:3000])
        self.assertIn('saved=added', response.url)
        page = self.client.get(response.url).content.decode()
        self.assertIn('<div class="saved-message" role="status">Customer added.</div>', page)
        page = self.client.get('/customers/?saved=1').content.decode()
        self.assertIn('<div class="saved-message" role="status">Customer saved.</div>', page)
        self.assertNotIn('class="saved-message" role="status"', self.client.get('/customers/').content.decode())


class OrderPaymentSavedNoteTests(TestCase):
    def setUp(self):
        customer = Customer.objects.create(source_id='r81', full_name='TEST R81')
        item = Item.objects.create(sku='R81', name='R81 CHAIR', description='Chair', price='100.00', tax_rate='7.000')
        self.order = Document.objects.create(kind='order', status='open', customer=customer, customer_name='TEST R81',
                                             subtotal='100.00', tax='7.00', total='107.00')
        Line.objects.create(document=self.order, position=0, item=item, description='Chair', quantity=1, price='100.00')

    def post(self, **data):
        self.order.refresh_from_db()
        return self.client.post(f'/pos/order/{self.order.token}/payment/',
                                {'version': self.order.version, 'token': str(uuid.uuid4()), 'method': 'cash', **data})

    def test_recording_correcting_and_deleting_a_payment_each_say_so(self):
        response = self.post(amount='20.00')
        self.assertEqual(response.url, f'/pos/order/{self.order.token}/?saved=payment-recorded')
        page = self.client.get(response.url).content.decode()
        self.assertIn('<div class="saved-note" id="saved-note" role="status">Payment recorded.</div>', page)
        payment = OrderPayment.objects.get()
        response = self.post(amount='25.00', payment_id=payment.pk, token=str(payment.token))
        self.assertEqual(response.url, f'/pos/order/{self.order.token}/?saved=payment-saved')
        response = self.post(action='delete', payment_id=payment.pk)
        self.assertEqual(response.url, f'/pos/order/{self.order.token}/?saved=payment-deleted')
        self.assertFalse(OrderPayment.objects.exists())

    def test_a_refused_payment_shows_the_error_not_a_note(self):
        response = self.post(amount='0')
        self.assertIn('?error=', response.url)
        self.assertNotIn('id="saved-note"', self.client.get(response.url).content.decode())

    def test_unknown_or_missing_note_shows_nothing(self):
        for query in ('', '?saved=whatever'):
            self.assertNotIn('id="saved-note"', self.client.get(f'/pos/order/{self.order.token}/{query}').content.decode())
