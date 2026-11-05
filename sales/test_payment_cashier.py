# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): order deposits, payments and refunds record who took them, like QB's deposit receipts."""
import uuid
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Cashier
from accounts.tests import MIDDLEWARE, make_admin, make_register
from customers.models import Customer
from .models import Document, Line, OrderPayment, OrderPaymentChange
from .services import save_document


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class OrderPaymentCashierTests(TestCase):
    def setUp(self):
        self.register = make_register()
        self.admin = make_admin()
        self.cashier = Cashier.objects.create(name='TEST Cashier', pin_hash=make_password('0123'))
        self.other = Cashier.objects.create(name='TEST Other', pin_hash=make_password('4567'))
        self.customer = Customer.objects.create(source_id='C-R61', full_name='TEST R61 CUSTOMER')
        self.order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='TEST R61 CUSTOMER',
                                             subtotal='100.00', tax='7.00', total='107.00', tax_location='Springfield')
        Line.objects.create(document=self.order, position=0, quantity=1, price=100, tax_rate=7)
        self.client.force_login(self.register)

    def activate(self, cashier, pin):
        self.client.post('/cashier/', {'pin': pin, 'cashier': cashier.pk})

    def pay(self, amount, **extra):
        self.order.refresh_from_db()
        data = dict(version=self.order.version, token=str(uuid.uuid4()), amount=amount, method='cash', **extra)
        response = self.client.post(f'/pos/order/{self.order.token}/payment/', data)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('error=', response['Location'])
        return OrderPayment.objects.filter(order=self.order).latest('pk')

    def test_new_payment_and_refund_stamp_the_active_cashier(self):
        self.activate(self.cashier, '0123')
        deposit = self.pay('50.00')
        self.assertEqual(deposit.cashier, 'TEST Cashier')
        refund = self.pay('-10.00')
        self.assertEqual(refund.amount, -10)
        self.assertEqual(refund.cashier, 'TEST Cashier')

    def test_signed_in_admin_is_the_cashier(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.pay('20.00').cashier, self.admin.get_full_name() or self.admin.get_username())

    def test_no_active_cashier_leaves_it_blank(self):
        self.assertEqual(self.pay('20.00').cashier, '')

    def test_same_day_correction_keeps_the_original_cashier(self):
        self.activate(self.cashier, '0123')
        payment = self.pay('50.00')
        self.activate(self.other, '4567')
        self.order.refresh_from_db()
        response = self.client.post(f'/pos/order/{self.order.token}/payment/',
                                    dict(version=self.order.version, token=str(payment.token), payment_id=payment.pk, amount='40.00', method='cash'))
        self.assertNotIn('error=', response['Location'])
        payment.refresh_from_db()
        self.assertEqual(payment.amount, 40)
        self.assertEqual(payment.cashier, 'TEST Cashier')
        change = OrderPaymentChange.objects.get(payment=payment)
        self.assertEqual(change.cashier, 'TEST Other')  # the change log records who corrected it

    def test_cancel_refund_stamps_the_cashier(self):
        OrderPayment.objects.create(order=self.order, amount=50, method='cash')
        self.order.refresh_from_db()
        save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method='cash',
                           deposit_refund_amount='50'), 'order', self.order.token, cashier='TEST Cashier')
        refund = self.order.payments.filter(amount__lt=0, adjustment=False).get()
        self.assertEqual(refund.cashier, 'TEST Cashier')
        # The tax-adjustment rows (if the cancel booked one) stay blank.
        self.assertFalse(self.order.payments.filter(adjustment=True).exclude(cashier='').exists())

    def test_cancel_refund_with_no_cashier_is_blank(self):
        OrderPayment.objects.create(order=self.order, amount=50, method='cash')
        self.order.refresh_from_db()
        save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method='cash',
                           deposit_refund_amount='50'), 'order', self.order.token)
        self.assertEqual(self.order.payments.filter(amount__lt=0, adjustment=False).get().cashier, '')

    def test_order_page_shows_who_took_it(self):
        self.activate(self.cashier, '0123')
        self.pay('50.00')
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertContains(page, '<span class="payment-cashier">Cashier TEST Cashier</span>', html=False)
        # An earlier-day payment row shows it too.
        OrderPayment.objects.filter(order=self.order).update(created_at=timezone.now() - timedelta(days=2))
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertContains(page, 'earlier-day')
        self.assertContains(page, '<span class="payment-cashier">Cashier TEST Cashier</span>', html=False)

    def test_sales_history_shows_and_sorts_by_cashier(self):
        self.client.force_login(self.admin)  # Sales History; the row data doesn't depend on who views it
        OrderPayment.objects.create(order=self.order, amount='30.00', method='cash', cashier='TEST Zed',
                                    created_at=timezone.now() - timedelta(hours=2))
        OrderPayment.objects.create(order=self.order, amount='20.00', method='cash', cashier='TEST Amy',
                                    created_at=timezone.now() - timedelta(hours=1))
        sale = Document.objects.create(kind='sale', status='completed', customer=self.customer, customer_name='TEST R61 CUSTOMER',
                                       subtotal='5', tax='0', total='5', payment_method='cash', tendered='5', was_completed=True,
                                       completed_at=timezone.now() - timedelta(hours=3), cashier='TEST Mid')
        page = self.client.get('/pos/history/')
        self.assertContains(page, '<th>Cashier</th>', html=False)
        self.assertContains(page, '<td>TEST Zed</td>', html=False)
        self.assertContains(page, '<td>TEST Mid</td>', html=False)
        names = lambda sort: [row.cashier for row in self.client.get('/pos/history/', {'sort': sort}).context['page'].object_list]
        self.assertEqual(names('cashier'), ['TEST Amy', 'TEST Mid', 'TEST Zed'])
        self.assertEqual(names('-cashier'), ['TEST Zed', 'TEST Mid', 'TEST Amy'])
        self.assertIn('cashier', self.client.get('/pos/history/').context['sort_fields'])
        rows = self.client.get('/pos/history/', {'format': 'rows', 'offset': '0', 'sort': 'cashier'}).json()['rows']
        self.assertEqual([row['cells'][3] for row in rows], ['TEST Amy', 'TEST Mid', 'TEST Zed'])
        self.assertEqual(rows[1]['id'], sale.pk)
