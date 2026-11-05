# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a Check payment or refund on a sales order records its check number, shown as "Check #1470"
wherever the order's payments are shown, like a sale's check tender."""
from decimal import Decimal as D
from unittest import mock

from django.test import override_settings

from sales.history import PaymentRow
from sales.letter_printing import deposit_rows
from sales.models import OrderPayment, OrderPaymentChange
from sales.printing import Receipt, render_receipt
from sales.services import WorkflowError, EARLIER_DAY_MESSAGE
from sales.test_imported_order_review import store_day
from sales.test_order_payment_tax import Part4Base, YDAY, TODAY


class CheckNumberOnOrderPayments(Part4Base):
    def test_new_check_payment_needs_and_shows_its_number(self):
        with self.assertRaisesMessage(WorkflowError, 'Enter the check number.'):
            self.pay('70', method='check')
        with self.assertRaisesMessage(WorkflowError, 'Enter the check number.'):
            self.pay('70', method='check', check_number='  # ')
        with self.assertRaisesMessage(WorkflowError, 'letters, digits or dashes only'):
            self.pay('70', method='check', check_number='14;70')
        with self.assertRaisesMessage(WorkflowError, 'Check number'):
            self.pay('70', method='check', check_number='1' * 21)
        p = self.pay('70', method='check', check_number=' #1470 ', reference='Mailed in')
        self.assertEqual((p.method, p.check_number, p.label, p.reference), ('check', '1470', 'Check #1470', 'Mailed in'))
        self.assertIn(('Mailed in', D('70.00'), 'Check #1470'), [row[1:] for row in deposit_rows(self.order)])
        self.assertEqual(PaymentRow(p).payment_label, 'Check #1470')       # Sales History

    def test_check_refund_needs_a_number_too(self):
        self.app_only()
        self.pay('100', method='cash')
        with self.assertRaisesMessage(WorkflowError, 'Enter the check number.'):
            self.pay('-20', method='check')
        refund = self.pay('-20', method='check', check_number='5012')
        self.assertEqual(refund.label, 'Check #5012')

    def test_other_methods_drop_a_check_number(self):
        cash = self.pay('70', method='cash', check_number='1470')
        self.assertEqual((cash.check_number, cash.label), ('', 'Cash'))
        p = self.pay('10', method='check', check_number='1470')
        p = self.correct(p, method='credit', card_type='Visa', check_number='1470')
        self.assertEqual((p.method, p.check_number, p.label), ('credit', '', 'Visa'))

    def test_same_day_correction_fixes_the_number_without_relocking(self):
        p = self.pay('70', method='check', check_number='1407')
        locked = p.locked_tax
        p = self.correct(p, check_number='1470')
        self.assertEqual((p.check_number, p.locked_tax, p.label), ('1470', locked, 'Check #1470'))
        self.assertEqual(OrderPaymentChange.objects.get().summary, f'Corrected Check #1407 $70.00 dated {TODAY.date()}: Check # 1407 -> 1470')
        with self.assertRaisesMessage(WorkflowError, 'Enter the check number.'):
            self.correct(p, check_number='')
        p.refresh_from_db()
        self.assertEqual(p.check_number, '1470')

    def test_earlier_day_check_number_is_a_reference_like_edit(self):
        p = self.pay('70', method='check', check_number='1407', when=YDAY)
        locked = p.locked_tax
        p = self.correct(p, check_number='1470')
        self.assertEqual((p.check_number, p.locked_tax, p.amount), ('1470', locked, D('70.00')))
        self.assertEqual(self.day_tax(YDAY), locked)
        with self.assertRaisesMessage(WorkflowError, EARLIER_DAY_MESSAGE):
            self.correct(p, amount='80', check_number='1470')

    def test_check_recorded_before_r92_keeps_working_without_a_number(self):
        """Old payments stay as they are: no backfill, their reference still saves, and they show as 'Check'."""
        with store_day(YDAY):
            old = OrderPayment.objects.create(order=self.order, amount=D('30'), method='check', reference='Check #1388', created_at=YDAY)
        self.assertEqual(old.label, 'Check')
        old = self.correct(old, reference='Check #1388 (mailed)')
        self.assertEqual((old.check_number, old.reference), ('', 'Check #1388 (mailed)'))
        old = self.correct(old, check_number='1388')     # may be given its number later
        self.assertEqual(old.label, 'Check #1388')

    def test_pickup_receipt_and_its_screen_show_the_check_number(self):
        self.app_only()
        self.pay('214', method='check', check_number='1470')
        receipt = self.pickup(2)
        texts = []
        original = Receipt.columns
        with mock.patch.object(Receipt, 'columns', autospec=True,
                               side_effect=lambda self, cells, *a, **k: (texts.append(' '.join(c[0] for c in cells)), original(self, cells, *a, **k))[1]):
            render_receipt(receipt)
        self.assertIn('Check #1470: $214.00', texts)
        self.assertEqual(receipt.payment_label, 'Order payments: Check #1470')
        with override_settings(POS_REQUIRE_LOGIN=False), store_day():
            page = self.client.get(f'/pos/sale/{receipt.token}/')
        self.assertContains(page, 'Check #1470 $214.00')

    @override_settings(POS_REQUIRE_LOGIN=False)
    def test_order_page_has_the_check_box_and_shows_the_number(self):
        self.app_only()
        self.pay('70', method='check', check_number='1470')
        with store_day():
            page = self.client.get(f'/pos/order/{self.order.token}/')
        html = page.content.decode()
        self.assertEqual(html.count('name="check_number"'), 2)          # new payment + today's row
        self.assertIn('<label class="check-number" data-required="1">Check # <input name="check_number" maxlength="20" value=""', html)
        self.assertIn('value="1470"', html)
        # The read-only payment list (order-history.html and Sales History rows) prints payment.label too.
        from django.template.loader import render_to_string
        rows = render_to_string('sales/order-history.html', {'doc': self.order, 'order_payments': list(self.order.payments.all())})
        self.assertIn('<td>Check #1470</td>', rows)
