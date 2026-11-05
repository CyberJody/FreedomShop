# SPDX-License-Identifier: AGPL-3.0-or-later
"""add-on: "The software should just give a warning if we're taking an overpayment… the
responsibility of the person running the cash register not to take an overpayment."

The order page asks before posting a payment that takes more than the balance due; the server refuses the same post
unless it carries confirm_overpayment=1, with the same words, so a stale page can't skip the question. Refunds, exact
and under-balance payments, reference-only edits and the tax rules are untouched.
"""
import uuid
from decimal import Decimal as D

from django.test import override_settings

from sales.models import OrderPayment
from sales.services import save_order_payment, WorkflowError, overpayment_message
from sales.test_imported_orders import ImportedOrderBase, TODAY
from sales.test_imported_order_review import store_day


class OverpaymentWarning(ImportedOrderBase):
    """Order 214.00 with QuickBooks' 50.00 deposit: balance due 164.00."""

    def correct(self, payment, **changes):
        self.order.refresh_from_db()
        data = dict(version=self.order.version, token=str(payment.token), payment_id=str(payment.pk), amount=str(payment.amount),
                    method=payment.method, reference=payment.reference)
        data.update(changes)
        with store_day(TODAY):   # same store day as pay pins, so this is a same-day correction
            save_order_payment(self.order.pk, data)
        payment.refresh_from_db()
        return payment

    def test_message_wording(self):
        self.assertEqual(overpayment_message(D('36.00'), D('164.00')),
                         'This is $36.00 more than the $164.00 balance due. Take the overpayment anyway?')

    def test_overpayment_refused_without_confirm(self):
        with self.assertRaises(WorkflowError) as caught:
            self.pay('200.00')
        self.assertEqual(str(caught.exception), 'This is $36.00 more than the $164.00 balance due. Take the overpayment anyway?')
        self.assertEqual(self.order.payments.count(), 0)

    def test_overpayment_accepted_with_confirm(self):
        payment = self.pay('200.00', confirm_overpayment='1')
        self.assertEqual(payment.amount, D('200.00'))
        self.order.refresh_from_db()
        self.assertEqual(self.order.balance_due, D('-36.00'))
        self.assertEqual(self.order.payments.filter(adjustment=False).count(), 1)

    def test_exact_and_under_balance_need_no_confirm(self):
        self.pay('100.00')
        self.order.refresh_from_db()
        self.assertEqual(self.order.balance_due, D('64.00'))
        self.pay('64.00')
        self.order.refresh_from_db()
        self.assertEqual(self.order.balance_due, D('0.00'))
        self.assertEqual(self.order.payments.filter(adjustment=False).count(), 2)

    def test_refund_needs_no_confirm(self):
        self.pay('164.00')
        payment = self.pay('-20.00')
        self.assertEqual(payment.amount, D('-20.00'))
        self.order.refresh_from_db()
        self.assertEqual(self.order.balance_due, D('20.00'))

    def test_one_cent_over_is_still_asked(self):
        with self.assertRaises(WorkflowError) as caught:
            self.pay('164.01')
        self.assertIn('$0.01 more than the $164.00 balance due', str(caught.exception))

    def test_same_day_correction_that_overpays_asks(self):
        payment = self.pay('100.00')
        with self.assertRaises(WorkflowError) as caught:
            self.correct(payment, amount='170.00')
        # Balance the page shows for this row: 64.00 due plus the 100.00 this payment already covers.
        self.assertEqual(str(caught.exception), 'This is $6.00 more than the $164.00 balance due. Take the overpayment anyway?')
        payment.refresh_from_db()
        self.assertEqual(payment.amount, D('100.00'))
        self.correct(payment, amount='170.00', confirm_overpayment='1')
        self.assertEqual(payment.amount, D('170.00'))

    def test_correction_within_balance_and_reference_only_need_no_confirm(self):
        payment = self.pay('100.00')
        self.correct(payment, amount='164.00')
        self.assertEqual(payment.amount, D('164.00'))
        self.correct(payment, reference='paid in full')  # money unchanged: no overpayment question
        self.assertEqual(payment.reference, 'paid in full')

    def test_payment_on_overpaid_order_asks(self):
        self.pay('164.00')
        with self.assertRaises(WorkflowError) as caught:
            self.pay('10.00')
        self.assertEqual(str(caught.exception), 'This is $10.00 more than the $0.00 balance due. Take the overpayment anyway?')


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class OverpaymentPage(ImportedOrderBase):
    """The order page carries the warning dialog and the view repeats the refusal as the page's error."""

    def test_page_has_dialog_and_confirm_fields(self):
        html = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
        self.assertIn('id="overpayment-dialog"', html)
        self.assertIn('Change amount', html)
        self.assertIn('Take overpayment', html)
        self.assertIn('id="order-balance-due" data-amount="164.00"', html)
        self.assertIn('name="confirm_overpayment"', html)

    def test_post_without_confirm_is_refused_then_accepted(self):
        url = f'/pos/order/{self.order.token}/payment/'
        data = dict(version=self.order.version, token=str(uuid.uuid4()), amount='200.00', method='cash', reference='')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('This+is+%2436.00+more+than+the+%24164.00+balance+due.+Take+the+overpayment+anyway%3F', response['Location'])
        self.assertEqual(OrderPayment.objects.count(), 0)
        response = self.client.post(url, dict(data, confirm_overpayment='1'))
        self.assertEqual(response['Location'], f'/pos/order/{self.order.token}/?saved=payment-recorded')  # note
        self.assertEqual(OrderPayment.objects.filter(adjustment=False).count(), 1)
