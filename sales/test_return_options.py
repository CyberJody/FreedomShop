# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accept Return options: "Damaged — don't restock", return reason + note,
optional restocking fee."""
from decimal import Decimal as D

from .models import Document, StockMovement
from .services import save_document
from .test_returns import AcceptReturnTests, ReturnTestCase


class OptionsTestCase(ReturnTestCase):
    printed = AcceptReturnTests.printed


class DamagedReturnTests(OptionsTestCase):
    def test_damaged_return_stays_out_of_stock(self):
        sale = self.sell('2')
        self.assertEqual(self.on_hand(self.chair), D('3'))
        ret = self.save([self.returned(sale, damaged=True)], [dict(method='cash', amount='-107.00')])
        line = ret.lines.get()
        self.assertTrue(line.damaged)
        self.assertEqual(self.on_hand(self.chair), D('3'))  # not back on the shelf
        self.assertFalse(StockMovement.objects.filter(document=ret).exists())
        self.assertEqual(ret.total, D('-107.00'))  # the customer is still refunded
        self.assertIn('Damaged - not restocked', self.printed(ret))
        page = self.client.get(f'/pos/sale/{ret.token}/').context['initial']['lines'][0]
        self.assertTrue(page['damaged'])
        # Same day: untick it -> the chair goes back into stock; tick it again -> out again.
        edited = self.save([dict(id=line.pk, quantity='-1', damaged=False)], [dict(method='cash', amount='-107.00')], token=str(ret.token), version=ret.version)
        self.assertEqual((edited.lines.get().damaged, self.on_hand(self.chair)), (False, D('4')))
        edited = self.save([dict(id=line.pk, quantity='-1', damaged=True)], [dict(method='cash', amount='-107.00')], token=str(ret.token), version=edited.version)
        self.assertEqual(self.on_hand(self.chair), D('3'))
        # Cancelling a damaged return changes no stock either.
        save_document(dict(action='cancel', version=edited.version), 'sale', ret.token)
        self.assertEqual(self.on_hand(self.chair), D('3'))

    def test_mixed_damaged_and_restocked_lines(self):
        sale = self.sell('2')
        ret = self.save([self.returned(sale, '-1', damaged=True), self.returned(sale, '-1')], [dict(method='cash', amount='-214.00')])
        self.assertEqual(self.on_hand(self.chair), D('4'))  # one back, one damaged
        self.assertEqual(StockMovement.objects.get(document=ret).quantity, D('1'))
        self.assertEqual(sorted(ret.lines.values_list('damaged', flat=True)), [False, True])

    def test_damaged_flag_ignored_on_sold_lines(self):
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00', damaged=True)], [dict(method='cash', amount='107.00')])
        self.assertFalse(doc.lines.get().damaged)
        self.assertEqual(self.on_hand(self.chair), D('4'))


class ReturnReasonTests(OptionsTestCase):
    def test_reason_and_note_are_stored_shown_and_printed(self):
        from .models import RETURN_REASONS
        from .views import sales_row
        self.assertEqual([label for _, label in RETURN_REASONS], ['Defective/Damaged', 'Changed mind', 'Wrong item', 'Exchange', 'Other'])
        sale = self.sell('2')
        ret = self.save([self.returned(sale, return_reason='changed_mind', return_note='  Too big for the room ')], [dict(method='cash', amount='-107.00')])
        line = ret.lines.get()
        self.assertEqual((line.return_reason, line.return_note, line.return_reason_text), ('changed_mind', 'Too big for the room', 'Changed mind - Too big for the room'))
        self.assertIn('Reason: Changed mind - Too big for the room', self.printed(ret))
        page = self.client.get(f'/pos/sale/{ret.token}/')
        self.assertEqual((page.context['initial']['lines'][0]['return_reason'], page.context['initial']['lines'][0]['return_note']), ('changed_mind', 'Too big for the room'))
        self.assertContains(page, '"return-reasons"')
        self.assertContains(page, 'Defective/Damaged')
        from django.db.models import Count, Sum
        listed = lambda doc: Document.objects.annotate(item_count=Count('lines'), item_quantity=Sum('lines__quantity')).get(pk=doc.pk)
        row = sales_row(listed(ret), 'history')
        self.assertEqual(row['cells'][2], 'Completed · Return (Changed mind - Too big for the room)')
        self.assertEqual(sales_row(listed(sale), 'history')['cells'][2], 'Completed')
        no_reason = self.save([self.returned(sale)], [dict(method='cash', amount='-107.00')])
        self.assertEqual(sales_row(listed(no_reason), 'history')['cells'][2], 'Completed · Return')

    def test_reason_must_come_from_the_list_and_only_on_returns(self):
        from .services import WorkflowError
        sale = self.sell('1')
        with self.assertRaisesMessage(WorkflowError, 'Choose a return reason from the list.'):
            self.save([self.returned(sale, return_reason='Changed mind')], [dict(method='cash', amount='-107.00')])
        with self.assertRaisesMessage(WorkflowError, 'Return note must be no more than 200 characters.'):
            self.save([self.returned(sale, return_note='x' * 201)], [dict(method='cash', amount='-107.00')])
        sold = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00', return_reason='other', return_note='n')], [dict(method='cash', amount='107.00')])
        self.assertEqual((sold.lines.get().return_reason, sold.lines.get().return_note), ('', ''))


class RestockingFeeTests(OptionsTestCase):
    """Optional 20% restocking fee. Tax treatment: the fee is taxable at the returned items' rate (the
    store's QB data rang "Restocking fee" at 7%): the customer gets back the item's price + tax minus the fee + its
    tax. Reports: the fee is part of the return receipt, so Daily Totals / Sales Tax show the receipt net of it;
    General Sales lists inventory items only, so the fee is not in its item totals (like other non-item lines)."""

    def test_fee_line_tax_and_reports(self):
        from reports import general_sales, ny_periods, sales_tax
        from .models import RESTOCKING_FEE_PERCENT
        from .test_returns import RETURN_DAY, SALE_DAY, day
        self.assertEqual(RESTOCKING_FEE_PERCENT, D('20'))
        sale = self.sell('2')
        ret = self.at_return_day(self.save([self.returned(sale)], [dict(method='cash', amount='-85.60')], restocking_fee=True))
        fee = ret.lines.get(restocking_fee=True)
        self.assertEqual((fee.item_id, fee.item_name, fee.description, fee.quantity, fee.price, fee.tax_rate),
                         (None, 'Restocking fee', 'Restocking fee (20% of returned items)', D('1'), D('20.00'), D('7')))
        self.assertEqual((ret.subtotal, ret.tax, ret.total), (D('-80.00'), D('-5.60'), D('-85.60')))  # -100 + 20; tax 7% of -80
        self.assertEqual(self.on_hand(self.chair), D('4'))  # the chair still goes back into stock
        # Daily Totals: the return's row is the refund net of the fee; its tax is the net tax.
        totals, rows = self.daily(RETURN_DAY.date())
        row = rows[ret.numbered_label]
        self.assertEqual((row['received'], row['cash'], row['tax'], row['new_sale']), (D('-85.60'), D('-85.60'), D('-5.60'), D('-85.60')))
        # Sales Tax: the fee is taxable sales on the return day (gross/taxable -100 + 20).
        st = sales_tax.build(day(RETURN_DAY.date()), detailed=True)
        self.assertEqual((st.gross, st.taxable, st.tax, st.total), (D('-80.00'), D('-80.00'), D('-5.60'), D('-85.60')))
        both = ny_periods.resolve('custom', '', SALE_DAY.date(), SALE_DAY.date(), RETURN_DAY.date())
        st = sales_tax.build(both, detailed=True)
        self.assertEqual((st.gross, st.taxable, st.tax), (D('120.00'), D('120.00'), D('8.40')))  # 200 sold - 100 back + 20 fee
        gs = general_sales.build(both)
        self.assertEqual((gs.totals['quantity'], gs.totals['amount']), (D('1'), D('100.00')))  # items only; fee not an item
        texts = self.printed(ret)
        self.assertIn('Restocking fee', texts)
        self.assertIn('RECEIPT TOTAL: -$85.60', texts)
        page = self.client.get(f'/pos/sale/{ret.token}/').context['initial']
        self.assertTrue(page['restocking_fee'])
        self.assertEqual([l.get('restocking_fee') for l in page['lines']], [None])  # not an editable line
        self.assertEqual(page['restocking_fee_lines'][0]['amount'], '20.00')
        self.assertEqual(page['restocking_fee_percent'], '20')

    def test_off_by_default_server_computes_and_can_be_removed(self):
        sale = self.sell('2')
        plain = self.save([self.returned(sale)], [dict(method='cash', amount='-107.00')])
        self.assertFalse(plain.lines.filter(restocking_fee=True).exists())
        # A posted fee row (or a made-up amount) is ignored; only the flag counts and the server computes the fee.
        fake = dict(restocking_fee=True, description='Restocking fee', quantity='1', price='1.00', tax_rate='0')
        ret = self.save([self.returned(sale), fake], [dict(method='cash', amount='-85.60')], restocking_fee=True)
        self.assertEqual(list(ret.lines.filter(restocking_fee=True).values_list('price', flat=True)), [D('20.00')])
        # Same day: untick it -> the fee line goes and the refund is the full amount.
        line = ret.lines.get(restocking_fee=False)
        edited = self.save([dict(id=line.pk, quantity='-1'), fake], [dict(method='cash', amount='-107.00')], token=str(ret.token), version=ret.version)
        self.assertEqual((edited.total, edited.lines.count()), (D('-107.00'), 1))

    def test_fee_rules_half_up_rates_and_exempt(self):
        from .services import WorkflowError
        with self.assertRaisesMessage(WorkflowError, 'Add the returned items before applying a restocking fee.'):
            self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], [dict(method='cash', amount='107.00')], restocking_fee=True)
        odd = self.save([dict(item_id=self.lamp.pk, quantity='1', price='33.33'), dict(item_id=self.chair.pk, quantity='1', price='100.00', tax_rate='0')],
                        [dict(method='cash', amount='135.66')])
        lamp, chair = odd.lines.get(item=self.lamp), odd.lines.get(item=self.chair)
        ret = self.save([self.returned(lamp), self.returned(chair)], [dict(method='cash', amount='-108.53')], restocking_fee=True)
        fees = dict(ret.lines.filter(restocking_fee=True).values_list('tax_rate', 'price'))
        self.assertEqual(fees, {D('7'): D('6.67'), D('0'): D('20.00')})  # 20% of 33.33 = 6.666 -> 6.67 (half-up); one fee per rate
        # -33.33 - 100.00 + 6.67 + 20.00; tax on the net taxable subtotal rounds half-up to -1.87.
        self.assertEqual((ret.subtotal, ret.tax, ret.total), (D('-106.66'), D('-1.87'), D('-108.53')))
        exempt_sale = self.sell('1')
        Document.objects.filter(pk=exempt_sale.pk).update(tax_exempt=True)
        ex = self.save([self.returned(exempt_sale)], [dict(method='cash', amount='-80.00')], restocking_fee=True, tax_exempt=True)
        self.assertEqual((ex.subtotal, ex.tax, ex.total), (D('-80.00'), D('0.00'), D('-80.00')))
