# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB deposit receipts): a Cash payment on a sales order records the Amount received and shows the change
on the order and its printout. Display only: the payment amount, its tax lock and Daily Totals don't use it."""
import uuid
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.tests import MIDDLEWARE, make_register
from customers.models import Customer
from .letter_printing import deposit_rows
from .models import Document, Line, OrderPayment, OrderPaymentChange


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class OrderCashChangeTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='C-R67', full_name='TEST R67 CUSTOMER')
        self.order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='TEST R67 CUSTOMER',
                                             subtotal='1000.00', tax='70.00', total='1070.00', tax_location='Springfield')
        Line.objects.create(document=self.order, position=0, quantity=1, price=1000, tax_rate=7)
        self.client.force_login(make_register())

    def post(self, **data):
        self.order.refresh_from_db()
        data = {'version': self.order.version, 'token': str(uuid.uuid4()), 'method': 'cash', **data}
        return self.client.post(f'/pos/order/{self.order.token}/payment/', data)

    def pay(self, **data):
        response = self.post(**data)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('error=', response['Location'], response['Location'])
        return OrderPayment.objects.filter(order=self.order).latest('pk')

    def test_form_offers_amount_received_and_keeps_credit_card_default(self):
        page = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
        self.assertIn('name="tendered"', page)
        self.assertIn('Amount received', page)
        self.assertIn('<option value="credit" selected>', page)   # 86% of deposits are cards
        self.assertIn("'Change '+money(got-amount)", page)       # the live change note

    def test_cash_deposit_records_received_and_change_but_not_as_money(self):
        payment = self.pay(amount='500.00', tendered='600.00')
        self.assertEqual((payment.amount, payment.tendered, payment.change_given), (D('500.00'), D('600.00'), D('100.00')))
        self.order.refresh_from_db()
        self.assertEqual(self.order.paid, D('500.00'))
        # The tax lock is for the $500 payment, the same as a payment with no Amount received.
        same = self.pay(amount='500.00', confirm_overpayment='1')
        self.assertIsNone(same.tendered)
        self.assertEqual((payment.locked_tax, payment.locked_taxable, payment.locked_nontaxable),
                         (same.locked_tax, same.locked_taxable, same.locked_nontaxable))
        self.assertEqual(payment.locked_tax + payment.locked_taxable + payment.locked_nontaxable, D('500.00'))

    def test_order_row_and_printout_show_cash_and_change(self):
        payment = self.pay(amount='445.65', tendered='500')
        page = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
        self.assertIn('value="500.00" aria-label="Cash amount received"', page)   # today's row, editable
        OrderPayment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=2))
        page = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
        self.assertIn('<span class="cash-change-note">Cash $500.00 · Change $54.35</span>', page)
        self.assertEqual(deposit_rows(self.order)[-1][2:], (D('445.65'), 'Cash $500.00 · Change $54.35'))

    def test_received_is_optional_and_only_for_cash(self):
        self.assertIsNone(self.pay(amount='100.00').tendered)
        self.assertEqual(deposit_rows(self.order)[-1][3], 'Cash')
        card = self.pay(amount='100.00', method='credit', tendered='600.00')
        self.assertIsNone(card.tendered)
        self.assertEqual(deposit_rows(self.order)[-1][3], 'Credit card')
        refund = self.pay(amount='-50.00', tendered='60.00')
        self.assertIsNone(refund.tendered)

    def test_received_below_the_payment_is_refused(self):
        response = self.post(amount='500.00', tendered='400.00')
        self.assertIn('error=', response['Location'])
        self.assertIn('amount+received', response['Location'])
        self.assertFalse(OrderPayment.objects.exists())
        self.assertIn('error=', self.post(amount='500.00', tendered='600.005')['Location'])

    def test_same_day_correction_changes_received_and_logs_it_without_relocking(self):
        payment = self.pay(amount='500.00', tendered='600.00')
        locked = payment.locked_tax
        self.order.refresh_from_db()
        response = self.client.post(f'/pos/order/{self.order.token}/payment/', dict(
            version=self.order.version, token=str(payment.token), payment_id=payment.pk, amount='500.00', method='cash',
            tendered='520.00', reference=''))
        self.assertNotIn('error=', response['Location'])
        payment.refresh_from_db()
        self.assertEqual((payment.tendered, payment.change_given, payment.locked_tax), (D('520.00'), D('20.00'), locked))
        self.assertIn('Amount received 600.00 -> 520.00', OrderPaymentChange.objects.get().summary)

    def test_earlier_day_reference_edit_keeps_received(self):
        payment = self.pay(amount='500.00', tendered='600.00')
        OrderPayment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.order.refresh_from_db()
        self.client.post(f'/pos/order/{self.order.token}/payment/', dict(
            version=self.order.version, token=str(payment.token), payment_id=payment.pk, amount='500.00', method='cash',
            reference='envelope 4'))
        payment.refresh_from_db()
        self.assertEqual((payment.reference, payment.tendered), ('envelope 4', D('600.00')))
