# SPDX-License-Identifier: AGPL-3.0-or-later
"""a whole-order % discount on sales orders, the way receipts take one, carried onto pickups."""
import json
import uuid
from decimal import Decimal as D

from django.test import TestCase

from customers.models import Customer
from reports.tax_ledger import order_taxable_shares
from .models import DiscountReason, Document, Item, OrderPayment, allocate_receipt_discount, tax_amount
from .services import WorkflowError, fulfill_order, save_document, save_order_payment
from .test_qb_half_up import register_math, run_node
from .views import document_data


class OrderDiscountTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='r46', full_name='Pat Buyer')
        self.chair = Item.objects.create(sku='6045', name='TEST CHAIR', description='TEST CHAIR', price='1209.95', on_hand='5', tax_rate='7.000', average_cost='500')
        self.bench = Item.objects.create(sku='6046', name='TEST BENCH', description='TEST BENCH', price='529.95', on_hand='3', tax_rate='7.000', average_cost='200')
        self.table = Item.objects.create(sku='6047', name='TEST TABLE', description='TEST TABLE', price='1369.95', on_hand='3', tax_rate='7.000', average_cost='600')
        DiscountReason.objects.get_or_create(name='Sale')

    # QB sales order 5388 (10/9/2026): 2 x 1,209.95 + 529.95 + 1,369.95, 5 % off the order.
    # QB stored Subtotal 4,319.80, Discount 215.99, Tax 287.27, Total 4,391.08.
    def lines(self):
        return [dict(item_id=self.chair.pk, quantity='2', price='1209.95'), dict(item_id=self.bench.pk, quantity='1', price='529.95'),
                dict(item_id=self.table.pk, quantity='1', price='1369.95')]

    def order(self, token=None, version=0, **extra):
        token = token or uuid.uuid4()
        data = dict(action='save', version=version, customer_id=self.customer.pk, lines=self.lines(), discount_percent='5', discount_reason='Sale', discount_note='Staff pick')
        data.update(extra)
        return save_document(data, 'order', token)

    def test_order_discount_matches_qb_order_5388(self):
        order = self.order()
        self.assertEqual((order.subtotal, order.discount, order.tax, order.total), (D('4319.80'), D('215.99'), D('287.27'), D('4391.08')))
        self.assertEqual((order.discount_percent, order.discount_reason, order.discount_note), (D('5'), 'Sale', 'Staff pick'))
        # Leftover cent on the largest line (2 x 1,209.95): 120.99 instead of the half-up 121.00; the others as rounded.
        self.assertEqual([l.receipt_discount for l in order.lines.all()], [D('120.99'), D('26.50'), D('68.50')])
        self.assertEqual(sum(l.net_amount for l in order.lines.all()), D('4103.81'))
        # Deposits, balance and the suggested deposit come off the discounted total.
        self.assertEqual((order.balance_due, order.unused_payments, order.paid), (D('4391.08'), D('0.00'), D('0.00')))
        response = self.client.get(f'/pos/order/{order.token}/')
        self.assertEqual(response.context['suggested_deposit'], D('1097.77'))
        self.assertContains(response, 'Order discount <select id="receipt-discount-type"')
        self.assertContains(response, 'id="receipt-discount"')
        payload = document_data(order)
        self.assertEqual((payload['discount'], payload['discount_percent'], payload['discount_reason']), ('215.99', '5.00', 'Sale'))
        # Change the percent: shares, tax and total follow; remove it: everything back to gross.
        order = self.order(token=order.token, version=order.version, discount_percent='15')
        self.assertEqual((order.discount, order.tax, order.total), (D('647.97'), D('257.03'), D('3928.86')))
        order = self.order(token=order.token, version=order.version, discount_percent='0', discount_reason='')
        self.assertEqual((order.discount, order.tax, order.total), (D('0.00'), D('302.39'), D('4622.19')))
        self.assertEqual([l.receipt_discount for l in order.lines.all()], [D('0.00')] * 3)

    def test_order_discount_needs_a_reason_and_tax_exempt_orders_take_it_too(self):
        with self.assertRaisesMessage(WorkflowError, 'Choose a reason for the order discount.'):
            self.order(discount_reason='')
        with self.assertRaisesMessage(WorkflowError, 'active discount reason'):
            self.order(discount_reason='Made up')
        exempt = self.order(tax_exempt=True)
        self.assertEqual((exempt.discount, exempt.tax, exempt.total), (D('215.99'), D('0.00'), D('4103.81')))

    def test_register_totals_an_order_discount_like_the_server_and_qb(self):
        lines = [dict(quantity='2', price='1209.95', tax_rate='7'), dict(quantity='1', price='529.95', tax_rate='7'), dict(quantity='1', price='1369.95', tax_rate='7')]
        script = (register_math() + '\nconst lines=' + json.dumps(lines) + ';'
                  'console.log(JSON.stringify([qbTotals(lines,"5","order",false),qbTotals(lines,"5","sale",false),qbTotals(lines,"5","order",true)].map(t=>[t.sub,t.discount,t.tax,t.total])));')
        order, sale, exempt = json.loads(run_node(script))
        self.assertEqual(order, [431980, 21599, 28727, 439108])
        self.assertEqual(sale, order)
        self.assertEqual(exempt, [431980, 21599, 0, 410381])
        # Per-line shares in the register equal the server's allocation (leftover cent on the largest line).
        script = (register_math() + '\nconst lines=' + json.dumps(lines) + ';'
                  'const s=receiptShares(lines,"5","order");console.log(JSON.stringify(lines.map(l=>Number(s.get(l)))));')
        self.assertEqual(json.loads(run_node(script)), [12099, 2650, 6850])
        self.assertEqual(allocate_receipt_discount([D('2419.90'), D('529.95'), D('1369.95')], D('5')), [D('120.99'), D('26.50'), D('68.50')])

    def pay(self, order, amount):
        save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount=str(amount), method='cash'))
        order.refresh_from_db()
        return order

    def pick(self, order, **quantities):
        order.refresh_from_db()
        receipt = fulfill_order(order.pk, dict(version=order.version, token=uuid.uuid4(), **{f'qty_{order.lines.get(item=getattr(self, name)).pk}': qty for name, qty in quantities.items()}))
        order.refresh_from_db()
        return receipt

    def test_full_pickup_receipt_carries_the_order_discount(self):
        order = self.pay(self.order(), '4391.08')
        receipt = self.pick(order, chair='2', bench='1', table='1')
        self.assertEqual((receipt.subtotal, receipt.discount, receipt.tax, receipt.total), (D('4319.80'), D('215.99'), D('287.27'), D('4391.08')))
        self.assertEqual((receipt.discount_percent, receipt.discount_reason, receipt.discount_note), (D('5'), 'Sale', 'Staff pick'))
        self.assertEqual([l.receipt_discount for l in receipt.lines.all()], [D('120.99'), D('26.50'), D('68.50')])
        self.assertEqual((order.status, order.balance_due, order.unused_payments), ('completed', D('0.00'), D('0.00')))
        # Stock moved for the picked-up quantities at the order's prices.
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.on_hand, 3)
        # The receipt prints Subtotal - Discount + Tax = Total like any discounted receipt.
        from .printing import Receipt, render_receipt
        from unittest import mock
        drawn = []
        original = Receipt.text

        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(receipt)
        text = ' '.join(drawn)
        self.assertIn('Discount:', drawn)
        self.assertIn('- $215.99', drawn)
        self.assertIn('Receipt discount: Sale - Staff pick', text)
        self.assertIn('RECEIPT TOTAL:', drawn)
        self.assertIn('$4,391.08', drawn)

    def test_partial_pickups_take_their_lines_shares_and_add_up_to_the_order(self):
        order = self.pay(self.order(), '4391.08')
        first = self.pick(order, chair='1', bench='1')      # one of the two chairs: half the chair line's share, 60.50
        self.assertEqual([l.receipt_discount for l in first.lines.all()], [D('60.50'), D('26.50')])
        self.assertEqual((first.subtotal, first.discount, first.tax, first.total), (D('1739.90'), D('87.00'), D('115.70'), D('1768.60')))
        self.assertEqual(order.status, 'open')
        second = self.pick(order, chair='1', table='1')     # the last chair takes the rest of its line's share: 60.49
        self.assertEqual([l.receipt_discount for l in second.lines.all()], [D('60.49'), D('68.50')])
        # Its own tax would round to 171.56 (2,450.91 x 7 % = 171.5637); the last pickup squares the order's tax instead:
        # 287.27 - 115.70 = 171.57, so the pickup receipts add up to the order to the cent.
        self.assertEqual((second.subtotal, second.discount, second.tax, second.total), (D('2579.90'), D('128.99'), D('171.57'), D('2622.48')))
        self.assertEqual(first.discount + second.discount, order.discount)
        self.assertEqual(first.subtotal + second.subtotal, order.subtotal)
        self.assertEqual(first.tax + second.tax, order.tax)
        self.assertEqual(first.total + second.total, order.total)
        self.assertEqual((order.status, order.unused_payments), ('completed', D('0.00')))
        # The tax ledger's order share nets the discount: taxable base = subtotal - discount.
        self.assertEqual(order_taxable_shares([order.pk])[order.pk], D('4103.81'))

    def test_pickup_is_refused_when_the_discounted_total_is_not_yet_paid(self):
        order = self.pay(self.order(), '4391.07')
        with self.assertRaisesMessage(WorkflowError, 'Record another $0.01'):
            self.pick(order, chair='2', bench='1', table='1')
        order = self.pay(order, '0.01')
        self.pick(order, chair='2', bench='1', table='1')
        self.assertEqual(order.status, 'completed')

    def test_pickup_rings_a_line_percent_order_line_at_its_order_price(self):
        # A line with its own % off keeps a 4-decimal price (1,209.95 x 90 % = 1,088.955); the pickup takes it as sold
        # even after the catalog price changes, and the line's % and reason show on the receipt.
        order = save_document(dict(action='save', version=0, customer_id=self.customer.pk,
                                   lines=[dict(item_id=self.chair.pk, quantity='1', price='1209.95', discount_percent='10', discount_reason='Sale')]), 'order', uuid.uuid4())
        line = order.lines.get()
        self.assertEqual((line.price, line.amount, order.total), (D('1088.9550'), D('1088.96'), D('1165.19')))
        self.chair.price = D('1300'); self.chair.save()
        order = self.pay(order, '1165.19')
        receipt = self.pick(order, chair='1')
        picked = receipt.lines.get()
        self.assertEqual((picked.price, picked.original_price, picked.discount_percent, picked.discount_reason, receipt.total), (D('1088.9550'), D('1209.95'), D('10'), 'Sale', D('1165.19')))

    def test_order_discount_survives_a_turned_off_reason_at_pickup(self):
        order = self.pay(self.order(), '4391.08')
        DiscountReason.objects.filter(name='Sale').update(active=False)
        receipt = self.pick(order, chair='2', bench='1', table='1')
        self.assertEqual((receipt.discount, receipt.discount_reason), (D('215.99'), 'Sale'))

    def test_letter_printout_shows_the_order_discount_and_each_lines_own_discount(self):
        from unittest import mock
        from .letter_printing import Pages, render_sales_order
        order = self.order()
        # One line with its own 10 % off as well (price 1,369.95 -> 1,232.955).
        rows = document_data(order)['lines']
        rows[2].update(discount_percent='10', discount_reason='Sale', discount_note='floor model')
        order = save_document(dict(action='save', version=order.version, customer_id=self.customer.pk, lines=rows, discount_percent='5', discount_reason='Sale', discount_note='Staff pick'), 'order', order.token)
        self.assertEqual((order.subtotal, order.discount, order.tax, order.total), (D('4182.81'), D('209.14'), D('278.16'), D('4251.83')))
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(order)
        self.assertEqual(len(pages), 1)
        texts = [step[3] for step in pages[0] if step[0] == 'text']
        for expected in ('Subtotal:', '$4,182.81', 'Discount (5 % Sale - Staff pick):', '- $209.14', 'Springfield 7 % Tax:', '+ $278.16', 'TOTAL:', '$4,251.83',
                         'Balance Due:', 'Discount: 10 % off $1,369.95 - Sale - floor model'):
            self.assertIn(expected, texts)
        # Totals read in order: Subtotal, Discount, Tax, TOTAL.
        order_of = [texts.index(t) for t in ('Subtotal:', 'Discount (5 % Sale - Staff pick):', 'Springfield 7 % Tax:', 'TOTAL:')]
        self.assertEqual(order_of, sorted(order_of))
        # The PDF route renders the same pages.
        from django.test import override_settings
        with override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware']):
            response = self.client.get(f'/print/order/{order.token}/pdf/')
        self.assertEqual((response.status_code, response['Content-Type'][:15]), (200, 'application/pdf'))
        # An order without a discount prints no Discount line at all.
        plain = save_document(dict(action='save', version=0, customer_id=self.customer.pk, lines=self.lines()), 'order', uuid.uuid4())
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            texts = [step[3] for step in render_sales_order(plain)[0] if step[0] == 'text']
        self.assertFalse([t for t in texts if t.startswith('Discount')])

    def test_last_pickup_squares_the_tax_with_the_order_without_a_discount_too(self):
        # Before the second pickup of 2 x 1,209.95 was refused: each half taxed 84.70 (84.6965 rounded) = 169.40
        # against the order's 169.39, one cent more than was paid ("Record another $0.01").
        order = save_document(dict(action='save', version=0, customer_id=self.customer.pk, lines=[dict(item_id=self.chair.pk, quantity='2', price='1209.95')]), 'order', uuid.uuid4())
        self.assertEqual((order.tax, order.total), (D('169.39'), D('2589.29')))
        order = self.pay(order, '2589.29')
        first = self.pick(order, chair='1')
        self.assertEqual((first.tax, first.total), (D('84.70'), D('1294.65')))
        second = self.pick(order, chair='1')
        self.assertEqual((second.tax, second.total), (D('84.69'), D('1294.64')))
        self.assertEqual((order.status, order.unused_payments, first.total + second.total), ('completed', D('0.00'), order.total))

    def test_split_pickups_of_a_fraction_of_a_cent_line_add_up_to_the_order(self):
        # 2 x (394.95 less 10 %) = 2 x 355.455 = 710.91 on the order; picked up one at a time each half would round to
        # 355.46 (710.92). The last pickup takes what is left of the line (355.45, kept as the recorded line total), so
        # subtotals, discount shares, tax and totals of the pickups equal the order's.
        self.chair.price = D('394.95'); self.chair.save()
        order = save_document(dict(action='save', version=0, customer_id=self.customer.pk, discount_percent='5', discount_reason='Sale',
                                   lines=[dict(item_id=self.chair.pk, quantity='2', price='394.95', discount_percent='10', discount_reason='Sale'), dict(item_id=self.bench.pk, quantity='1', price='529.95')]), 'order', uuid.uuid4())
        line = order.lines.get(item=self.chair)
        self.assertEqual((line.price, line.amount, line.receipt_discount, order.subtotal, order.discount, order.tax, order.total), (D('355.4550'), D('710.91'), D('35.54'), D('1240.86'), D('62.04'), D('82.52'), D('1261.34')))
        order = self.pay(order, '1261.34')
        first = self.pick(order, chair='1')
        self.assertEqual([(l.amount, l.recorded_amount, l.receipt_discount) for l in first.lines.all()], [(D('355.46'), None, D('17.77'))])
        second = self.pick(order, chair='1', bench='1')
        self.assertEqual([(l.amount, l.recorded_amount, l.receipt_discount) for l in second.lines.all()], [(D('355.45'), D('355.45'), D('17.77')), (D('529.95'), None, D('26.50'))])
        self.assertEqual((first.subtotal + second.subtotal, first.discount + second.discount, first.tax + second.tax, first.total + second.total), (order.subtotal, order.discount, order.tax, order.total))
        self.assertEqual((order.status, order.unused_payments), ('completed', D('0.00')))


class DollarOrderDiscountTests(OrderDiscountTests):
    """follow-up: a whole-cent dollar amount off the order instead of a percent, stored as `discount`
    with `discount_percent` 0 and spread over the eligible lines in proportion to their extensions (QB's spread of a
    typed dollar receipt discount:."""

    def dollar_order(self, amount='50', token=None, version=0, **extra):
        token = token or uuid.uuid4()
        data = dict(action='save', version=version, customer_id=self.customer.pk, lines=self.lines(), discount_percent='0', discount_amount=amount, discount_reason='Sale', discount_note='Staff pick')
        data.update(extra)
        return save_document(data, 'order', token)

    def test_dollar_discount_is_spread_by_extension_and_taxed_on_the_net(self):
        # 2 x 1,209.95 + 529.95 + 1,369.95 = 4,319.80; $50 off: 2,419.90/4,319.80 x 50 = 28.0087 -> 28.01, 529.95 -> 6.13,
        # 1,369.95 -> 15.86 (sum 50.00, no leftover). Tax 7 % on 4,269.80 = 298.886 -> 298.89; total 4,568.69.
        order = self.dollar_order()
        self.assertEqual((order.subtotal, order.discount, order.discount_percent, order.tax, order.total), (D('4319.80'), D('50.00'), D('0'), D('298.89'), D('4568.69')))
        self.assertEqual((order.discount_reason, order.discount_note), ('Sale', 'Staff pick'))
        self.assertEqual([l.receipt_discount for l in order.lines.all()], [D('28.01'), D('6.13'), D('15.86')])
        self.assertEqual(sum(l.net_amount for l in order.lines.all()), order.subtotal - order.discount)
        self.assertEqual((order.balance_due, self.client.get(f'/pos/order/{order.token}/').context['suggested_deposit']), (D('4568.69'), D('1142.17')))
        order = self.dollar_order('100.01', token=order.token, version=order.version)
        self.assertEqual([l.receipt_discount for l in order.lines.all()], [D('56.02'), D('12.27'), D('31.72')])
        self.assertEqual((order.discount, order.tax, order.total), (D('100.01'), D('295.39'), D('4515.18')))
        # Leftover cent: $0.05 -> 0.03 + 0.01 + 0.02 = 0.06, so the largest line gives one back (0.02, 0.01, 0.02).
        order = self.dollar_order('0.05', token=order.token, version=order.version)
        self.assertEqual([l.receipt_discount for l in order.lines.all()], [D('0.02'), D('0.01'), D('0.02')])
        self.assertEqual((order.discount, order.total), (D('0.05'), D('4622.13')))  # tax 302.3825 -> 302.38
        # Back to a percent, then to nothing: the fields follow.
        order = self.dollar_order('0', token=order.token, version=order.version, discount_percent='5')
        self.assertEqual((order.discount, order.discount_percent, order.total), (D('215.99'), D('5'), D('4391.08')))
        order = self.dollar_order('', token=order.token, version=order.version, discount_percent='', discount_reason='')
        self.assertEqual((order.discount, order.discount_percent, order.total), (D('0.00'), D('0'), D('4622.19')))
        self.assertEqual(allocate_receipt_discount([D('2419.90'), D('529.95'), D('1369.95')], D('0'), total=D('0.05')), [D('0.02'), D('0.01'), D('0.02')])

    def test_dollar_discount_refusals(self):
        with self.assertRaisesMessage(WorkflowError, 'Choose a reason for the order discount.'):
            self.dollar_order(discount_reason='')
        with self.assertRaisesMessage(WorkflowError, 'is more than the $4,319.80 of items it can come off'):
            self.dollar_order('4319.81')
        with self.assertRaisesMessage(WorkflowError, 'not both'):
            self.dollar_order('50', discount_percent='5')
        with self.assertRaisesMessage(WorkflowError, 'up to 2 decimal places'):
            self.dollar_order('50.005')
        with self.assertRaisesMessage(WorkflowError, 'valid order discount amount'):
            self.dollar_order('-5')
        # Receipts keep % only.
        with self.assertRaisesMessage(WorkflowError, 'A dollar discount is for sales orders'):
            save_document(dict(action='hold', version=0, customer_id=self.customer.pk, lines=self.lines(), discount_amount='50', discount_reason='Sale'), 'sale', uuid.uuid4())
        # The whole eligible subtotal is allowed (a free order), tax 0.
        free = self.dollar_order('4319.80')
        self.assertEqual((free.discount, free.tax, free.total), (D('4319.80'), D('0.00'), D('0.00')))

    def test_register_spreads_a_dollar_order_discount_like_the_server(self):
        lines = [dict(quantity='2', price='1209.95', tax_rate='7'), dict(quantity='1', price='529.95', tax_rate='7'), dict(quantity='1', price='1369.95', tax_rate='7')]
        script = (register_math() + '\nconst lines=' + json.dumps(lines) + ';'
                  'const t=[qbTotals(lines,"0","order",false,[],"50"),qbTotals(lines,"0","order",false,[],"100.01"),qbTotals(lines,"0","order",true,[],"50"),'
                  'qbTotals(lines,"0","order",false,[],"4319.81"),qbTotals(lines,"0","sale",false,[],"50"),qbTotals(lines,"","order",false,[],"")];'
                  'const s=receiptShares(lines,"0","order","0.05");'
                  'console.log(JSON.stringify([t.map(x=>[x.sub,x.discount,x.tax,x.total,x.valid,x.over]),lines.map(l=>Number(s.get(l)))]));')
        totals, shares = json.loads(run_node(script))
        self.assertEqual(totals[0], [431980, 5000, 29889, 456869, True, False])
        self.assertEqual(totals[1], [431980, 10001, 29539, 451518, True, False])
        self.assertEqual(totals[2], [431980, 5000, 0, 426980, True, False])
        # More than the items: no shares, flagged, saving blocked.
        self.assertEqual(totals[3][1:], [0, 30239, 462219, False, True])
        # A receipt ignores a dollar amount (receipts stay % only); an empty amount is no discount.
        self.assertEqual(totals[4], [431980, 0, 30239, 462219, True, False])
        self.assertEqual(totals[5], [431980, 0, 30239, 462219, True, False])
        self.assertEqual(shares, [2, 1, 2])

    def test_pickups_of_a_dollar_discounted_order_carry_the_shares_and_square_to_the_order(self):
        order = self.pay(self.dollar_order(), '4568.69')
        first = self.pick(order, chair='1', bench='1')      # half the chair line's 28.01 -> 14.01 (14.005 half-up), bench 6.13
        self.assertEqual([l.receipt_discount for l in first.lines.all()], [D('14.01'), D('6.13')])
        self.assertEqual((first.subtotal, first.discount, first.discount_percent, first.tax, first.total), (D('1739.90'), D('20.14'), D('0'), D('120.38'), D('1840.14')))
        second = self.pick(order, chair='1', table='1')     # the rest: 14.00 + 15.86; tax squared to the order
        self.assertEqual([l.receipt_discount for l in second.lines.all()], [D('14.00'), D('15.86')])
        self.assertEqual((first.discount + second.discount, first.tax + second.tax, first.total + second.total), (order.discount, order.tax, order.total))
        self.assertEqual((order.status, order.unused_payments), ('completed', D('0.00')))
        self.assertEqual((second.discount_reason, second.discount_note), ('Sale', 'Staff pick'))
        # Tax ledger: the order's taxable base nets the dollar discount.
        self.assertEqual(order_taxable_shares([order.pk])[order.pk], D('4269.80'))
        # The pickup receipt prints the discount line like any discounted receipt.
        from .printing import Receipt, render_receipt
        from unittest import mock
        drawn = []
        original = Receipt.text
        with mock.patch.object(Receipt, 'text', lambda self, text, *a, **k: (drawn.append(text), original(self, text, *a, **k))[1]):
            render_receipt(second)
        self.assertIn('Discount:', drawn)
        self.assertIn('- $29.86', drawn)
        self.assertIn('Receipt discount: Sale - Staff pick', ' '.join(drawn))

    def test_return_from_a_dollar_discounted_pickup_refunds_the_discounted_amount(self):
        order = self.pay(self.dollar_order(), '4568.69')
        receipt = self.pick(order, chair='2', bench='1', table='1')
        bench = receipt.lines.get(item=self.bench)
        # Return the bench (sold at 529.95 less its 6.13 share): refund 523.82 + 7 % tax on the net = 36.67 -> -560.49.
        rows = [dict(returned_from_line=bench.pk, item_id=self.bench.pk, quantity='-1', price='529.95', return_reason='changed_mind')]
        refund = save_document(dict(action='complete', version=0, customer_id=self.customer.pk, lines=rows, payments=[dict(method='cash', amount='-560.49')]), 'sale', uuid.uuid4())
        line = refund.lines.get()
        self.assertEqual((line.amount, line.receipt_discount, refund.discount, refund.tax, refund.total), (D('-529.95'), D('-6.13'), D('-6.13'), D('-36.67'), D('-560.49')))
        # The register gets the share and the sold amount with the returnable lines and mirrors the refund.
        response = self.client.get(f'/pos/returns/{receipt.token}/lines/')
        picked = [l for l in response.json()['lines'] if l['item_id'] == self.bench.pk][0]
        self.assertEqual((picked['discount_percent'], picked['discount_share'], picked['sold_amount']), ('0', '6.13', '529.95'))
        lines = [dict(returned_from_line=bench.pk, quantity='-1', price='529.95', tax_rate='7', return_limit='1', return_discount_percent='0', return_discount_share='6.13', return_sold_amount='529.95')]
        script = (register_math() + '\nconst lines=' + json.dumps(lines) + ';const t=qbTotals(lines,"0","sale",false);console.log(JSON.stringify([t.sub,t.discount,t.tax,t.total]));')
        self.assertEqual(json.loads(run_node(script)), [-52995, -613, -3667, -56049])

    def test_letter_printout_words_a_dollar_discount(self):
        from unittest import mock
        from .letter_printing import Pages, render_sales_order
        order = self.dollar_order()
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(order)
        self.assertEqual(len(pages), 1)
        texts = [step[3] for step in pages[0] if step[0] == 'text']
        for expected in ('Subtotal:', '$4,319.80', 'Discount ($50.00 Sale - Staff pick):', '- $50.00', 'Springfield 7 % Tax:', '+ $298.89', 'TOTAL:', '$4,568.69'):
            self.assertIn(expected, texts)
        self.assertFalse([t for t in texts if '%' in t and t.startswith('Discount')])

    def test_resaving_a_pickup_receipt_keeps_the_order_shares_and_tax(self):
        order = self.pay(self.dollar_order(), '4568.69')
        first = self.pick(order, chair='1', bench='1')
        second = self.pick(order, chair='1', table='1')     # tax squared: 298.89 - first.tax
        for receipt in (first, second):
            was = (receipt.discount, receipt.tax, receipt.total, [l.receipt_discount for l in receipt.lines.all()])
            data = document_data(receipt)
            self.assertTrue(data['pickup_receipt'])
            self.assertTrue(all(l['order_line'] for l in data['lines']))
            # Same-day re-save with a note (the register posts the document data back, percent 0, no amount).
            again = save_document(dict(data, action='complete', notes='Picked up by Pat', payments=[dict(method='order', amount=str(receipt.total))]), 'sale', receipt.token)
            self.assertEqual((again.discount, again.tax, again.total, [l.receipt_discount for l in again.lines.all()], again.notes), (*was, 'Picked up by Pat'))
        # The register shows the carried shares and the stored tax for the same lines (no re-allocation).
        data = document_data(second)
        script = (register_math() + '\nconst lines=' + json.dumps(data['lines']) + ';const t=qbTotals(lines,"0","sale",false);console.log(JSON.stringify([t.sub,t.discount,t.tax,t.total]));')
        sub, discount, tax, total = json.loads(run_node(script))
        self.assertEqual((sub, discount), (int(second.subtotal * 100), int(second.discount * 100)))
        self.assertEqual(discount, 1400 + 1586)
        # Its own tax rounds a cent differently from the squared one; the page keeps state.tax (totals in the register).
        self.assertEqual(abs(tax - int(second.tax * 100)) <= 1, True)
        # A %-order pickup keeps its shares on re-save too.
        pct_order = self.pay(self.order(), '4391.08')
        receipt = self.pick(pct_order, chair='2', bench='1', table='1')
        again = save_document(dict(document_data(receipt), action='complete', notes='x', payments=[dict(method='order', amount='4391.08')]), 'sale', receipt.token)
        self.assertEqual((again.discount, again.tax, again.total), (D('215.99'), D('287.27'), D('4391.08')))
