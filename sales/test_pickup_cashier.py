# SPDX-License-Identifier: AGPL-3.0-or-later
"""a pickup receipt (sales order picked up) records the active cashier, like any receipt (/rule)."""
import uuid
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings

from accounts.models import Cashier
from accounts.tests import MIDDLEWARE, make_admin, make_register
from customers.models import Customer
from .models import Document, Item
from .services import fulfill_order, save_document


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class PickupCashierTests(TestCase):
    def setUp(self):
        self.register = make_register()
        self.admin = make_admin()
        self.cashier = Cashier.objects.create(name='TEST Pickup Cashier', pin_hash=make_password('0123'))
        self.customer = Customer.objects.create(source_id='C-R73', full_name='TEST R73 CUSTOMER')
        self.item = Item.objects.create(sku='7301', name='TEST R73 CHAIR', description='TEST R73 CHAIR', price='100.00', on_hand='5', tax_rate='7.000')
        self.order = save_document(dict(action='save', customer_id=self.customer.pk,
                                        lines=[dict(item_id=self.item.pk, quantity='2', price='100.00', tax_rate='7')]), 'order', uuid.uuid4())
        self.client.force_login(self.register)

    def activate(self, cashier, pin):
        self.client.post('/cashier/', {'pin': pin, 'cashier': cashier.pk})

    def pickup(self):
        self.order.refresh_from_db()
        self.client.post(f'/pos/order/{self.order.token}/payment/', dict(version=self.order.version, token=str(uuid.uuid4()), amount='214.00', method='cash'))
        self.order.refresh_from_db()
        line = self.order.lines.get()
        response = self.client.post(f'/pos/order/{self.order.token}/fulfill/', {'version': self.order.version, 'token': str(uuid.uuid4()), f'qty_{line.pk}': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/pos/sale/'), response['Location'])
        return Document.objects.get(token=response['Location'].split('/')[3])

    def test_pin_cashier_is_stamped_and_shown_in_history_and_on_screen_not_printed(self):
        self.activate(self.cashier, '0123')
        receipt = self.pickup()
        self.assertEqual((receipt.source_order_id, receipt.cashier), (self.order.pk, 'TEST Pickup Cashier'))
        history = self.client.get('/pos/history/?period=all&status=all').content.decode()
        self.assertIn('TEST Pickup Cashier', history)
        page = self.client.get(f'/pos/sale/{receipt.token}/').content.decode()
        self.assertIn('Cashier TEST Pickup Cashier.', page)
        # The printed receipt never names the cashier.
        from .printing import Receipt, render_receipt
        drawn = []
        for name in ('text', 'wrap'):
            original = getattr(Receipt, name)
            def spy(self, text, *args, _original=original, **kwargs):
                drawn.append(str(text))
                return _original(self, text, *args, **kwargs)
            with mock.patch.object(Receipt, name, spy):
                render_receipt(receipt)
        self.assertFalse(any('TEST Pickup Cashier' in t for t in drawn))

    def test_signed_in_admin_is_the_cashier(self):
        self.client.force_login(self.admin)
        receipt = self.pickup()
        self.assertEqual(receipt.cashier, self.admin.get_full_name() or self.admin.get_username())

    def test_no_active_cashier_leaves_it_blank(self):
        receipt = self.pickup()
        self.assertEqual(receipt.cashier, '')
        self.assertNotIn('Cashier', self.client.get(f'/pos/sale/{receipt.token}/').content.decode().split('Pickup from', 1)[1].split('</div>', 1)[0])

    def test_service_default_is_blank(self):
        self.order.refresh_from_db()
        from .services import save_order_payment
        save_order_payment(self.order.pk, dict(version=self.order.version, token=str(uuid.uuid4()), amount='214.00', method='cash'))
        self.order.refresh_from_db()
        receipt = fulfill_order(self.order.pk, {'version': self.order.version, 'token': str(uuid.uuid4()), f'qty_{self.order.lines.get().pk}': '2'})
        self.assertEqual(receipt.cashier, '')
