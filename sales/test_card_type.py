# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): card type (Visa / MasterCard / American Express / Discover) on sales order payments."""
from decimal import Decimal as D
from unittest import mock

from django.test import override_settings

from sales.letter_printing import deposit_rows
from sales.models import OrderPaymentChange
from sales.printing import Receipt, render_receipt
from sales.services import WorkflowError, EARLIER_DAY_MESSAGE
from sales.test_imported_order_review import store_day
from sales.test_order_payment_tax import Part4Base, YDAY, TODAY


class CardTypeOnOrderPayments(Part4Base):
    def test_card_payment_stores_and_shows_its_card_type(self):
        p = self.pay('70', method='credit', card_type='MasterCard')
        self.assertEqual((p.method, p.card_type, p.label), ('credit', 'MasterCard', 'MasterCard'))
        self.assertIn('MasterCard', [row[3] for row in deposit_rows(self.order)])
        plain = self.pay('10', method='credit')
        self.assertEqual((plain.card_type, plain.label), ('', 'Credit card'))

    def test_unknown_card_type_refused_and_other_methods_drop_it(self):
        with self.assertRaisesMessage(WorkflowError, 'Choose Visa, MasterCard, American Express or Discover.'):
            self.pay('70', method='credit', card_type='Diners')
        cash = self.pay('70', method='cash', card_type='Visa')
        self.assertEqual((cash.card_type, cash.label), ('', 'Cash'))

    def test_same_day_correction_changes_card_type_and_method_change_clears_it(self):
        p = self.pay('70', method='credit', card_type='Visa')
        p = self.correct(p, card_type='Discover')
        self.assertEqual(p.card_type, 'Discover')
        self.assertIn('Card type Visa -> Discover', OrderPaymentChange.objects.get().summary)
        p = self.correct(p, method='check', check_number='1470')
        self.assertEqual((p.method, p.card_type, p.label), ('check', '', 'Check #1470'))

    def test_earlier_day_card_type_is_a_reference_like_edit(self):
        """Card type isn't money: an earlier day's card payment may get its card type, its tax lock never moves."""
        p = self.pay('70', method='credit', when=YDAY)
        locked = p.locked_tax
        p = self.correct(p, card_type='American Express')
        self.assertEqual((p.card_type, p.locked_tax, p.amount), ('American Express', locked, D('70.00')))
        self.assertEqual(self.day_tax(YDAY), locked)
        self.assertEqual(OrderPaymentChange.objects.get().summary,
                         f'Corrected Credit card $70.00 dated {YDAY.date()}: Card type (blank) -> American Express')
        with self.assertRaisesMessage(WorkflowError, EARLIER_DAY_MESSAGE):
            self.correct(p, amount='80')

    def test_pickup_receipt_prints_card_type(self):
        self.app_only()
        self.pay('214', method='credit', card_type='MasterCard')
        receipt = self.pickup(2)
        texts = []
        original = Receipt.columns
        with mock.patch.object(Receipt, 'columns', autospec=True,
                               side_effect=lambda self, cells, *a, **k: (texts.append(' '.join(c[0] for c in cells)), original(self, cells, *a, **k))[1]):
            render_receipt(receipt)
        self.assertIn('MasterCard: $214.00', texts)
        self.assertEqual(receipt.payment_label, 'Order payments: MasterCard')

    @override_settings(POS_REQUIRE_LOGIN=False)
    def test_order_page_offers_the_sale_screens_card_list(self):
        self.app_only()
        p = self.pay('70', method='credit', card_type='Visa')
        with store_day():
            page = self.client.get(f'/pos/order/{self.order.token}/')
        html = page.content.decode()
        self.assertEqual(html.count('<select name="card_type">'), 2)   # new payment + today's row
        for name in ('Visa', 'MasterCard', 'American Express', 'Discover'):
            self.assertIn(f'<option value="{name}"', html)
        self.assertIn('<option value="Visa" selected>Visa</option>', html)
