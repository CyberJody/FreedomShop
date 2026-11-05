# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal as D
from .test_returns import ReturnTestCase
from .models import DiscountReason, discount_text, allocate_receipt_discount, discounted_price, line_amount, tax_amount, unit_price
from .services import WorkflowError
from .views import document_data


class DiscountTests(ReturnTestCase):
    def test_receipt_discount_allocated_per_line_gross_subtotal_and_return(self):
        # QB's receipt: Subtotal gross 100, Discount 10 (on the line), tax on 90 = 6.30, Total 96.30.
        data = [dict(item_id=self.chair.pk, quantity='1', price='100', discount_reason='Sale', discount_note='TEST floor model')]
        doc = self.save(data, [dict(method='cash', amount='96.30')], discount_percent='10', discount_reason='Custom', discount_note='TEST receipt discount')
        self.assertEqual((doc.discount, doc.subtotal, doc.tax, doc.total), (D('10'), D('100'), D('6.30'), D('96.30')))
        line = doc.lines.get()
        self.assertEqual((line.price, line.receipt_discount, line.amount, line.net_amount, line.discount_reason), (D('100'), D('10'), D('100'), D('90'), 'Sale'))
        payload = document_data(doc)
        self.assertEqual((payload['lines'][0]['price'], payload['lines'][0]['display_price'], payload['discount']), ('100.00', '100.00', '10.00'))
        saved = self.save(payload['lines'], payload['payments'], token=doc.token, version=doc.version, discount_percent='10', discount_reason='Custom')
        self.assertEqual((saved.total, saved.subtotal), (D('96.30'), D('100')))  # editing does not apply the discount twice
        ret = self.save([self.returned(saved)], [dict(method='cash', amount='-96.30')])
        self.assertEqual(ret.total, D('-96.30'))

    def test_native_discount_report_does_not_deduct_from_taxable_twice(self):
        from reports import sales_tax, ny_periods
        from django.utils import timezone
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='100')],
                        [dict(method='cash', amount='96.30')], discount_percent='10', discount_reason='Sale')
        today=timezone.localdate(doc.completed_at)
        report=sales_tax.build(ny_periods.resolve('custom','',today,today,today))
        self.assertEqual((report.gross,report.discount,report.taxable,report.nontaxable,report.tax,report.total),
                         (D('100'),D('10'),D('90'),D('0'),D('6.30'),D('96.30')))

    def test_line_price_and_reason_preserved(self):
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='80', discount_reason='Defect', discount_note='TEST scratch')], [dict(method='cash', amount='85.60')])
        line = doc.lines.get()
        self.assertTrue(line.discounted)
        self.assertEqual(line.discount_note, 'TEST scratch')

    def test_reason_required_and_list_edit_keeps_history(self):
        with self.assertRaises(WorkflowError):
            self.save([dict(item_id=self.chair.pk, quantity='1', price='100')], [dict(method='cash', amount='100')], discount_percent='10')
        response = self.client.post('/pos/discount-reasons/', {'name': 'TEST reason'})
        self.assertEqual(response.status_code, 302)
        reason = DiscountReason.objects.get(name='TEST reason')
        self.client.post('/pos/discount-reasons/', {'id': reason.pk, 'name': 'TEST renamed', 'active': 'on'})
        self.assertTrue(DiscountReason.objects.filter(name='TEST renamed', active=True).exists())
        self.assertEqual(discount_text('Price Descrepancy', 'TEST'), 'Price Discrepancy - TEST')

    def test_receipt_percent_leftover_cent_goes_to_largest_line(self):
        # QB receipt 121467 (10/9/2026, 5 %): discount 623.71 on 12,474.15; plain half-up would give the
        # 10 x 349.95 = 3,499.50 line 174.98 — QB stored 174.96, absorbing the leftover. Tax 829.53 on the net.
        amounts = [D('1399.95'), D('1449.95'), D('374.95'), D('1349.95'), D('1199.90'), D('2399.95'), D('800.00'), D('3499.50')]
        shares = allocate_receipt_discount(amounts, D('5'))
        self.assertEqual(sum(shares), D('623.71'))
        self.assertEqual(shares, [D('70.00'), D('72.50'), D('18.75'), D('67.50'), D('60.00'), D('120.00'), D('40.00'), D('174.96')])
        self.assertEqual(tax_amount([(a - s, D('7')) for a, s in zip(amounts, shares)]), D('829.53'))
        # QB receipt 117922 (50 %): the largest line (1,249.95) absorbs -0.02; tax 92.22 on 1,317.40.
        amounts = [D('874.95'), D('264.95'), D('1249.95'), D('244.95')]
        shares = allocate_receipt_discount(amounts, D('50'))
        self.assertEqual(shares, [D('437.48'), D('132.48'), D('624.96'), D('122.48')])
        self.assertEqual(tax_amount([(a - s, D('7')) for a, s in zip(amounts, shares)]), D('92.22'))
        self.assertEqual(allocate_receipt_discount([D('10'), D('10')], D('0')), [D('0.00'), D('0.00')])
        # Ties: each 0.05 line gets half-up(0.005) = 0.01, the total is half-up(0.015) = 0.02, so the first of the
        # largest lines gives back the extra cent.
        self.assertEqual(allocate_receipt_discount([D('0.05'), D('0.05'), D('0.05')], D('10')), [D('0.00'), D('0.01'), D('0.01')])

    def test_receipt_percent_saved_with_leftover_and_mixed_lines(self):
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='0.05')] * 3, [dict(method='cash', amount='0.14')],
                        discount_percent='10', discount_reason='Sale')
        self.assertEqual([l.receipt_discount for l in doc.lines.all()], [D('0.00'), D('0.01'), D('0.01')])
        self.assertEqual((doc.subtotal, doc.discount, doc.tax, doc.total), (D('0.15'), D('0.02'), D('0.01'), D('0.14')))

    def test_line_percent_discount_keeps_exact_price_and_extends_from_it(self):
        # example from a shop's receipts: 1,379.95 at 10 % off = 1241.955 (shows 1,241.96);
        # 4 x 314.955 (349.95 at 10 % off) = 1,259.82, not 4 x 314.96 = 1,259.84.
        self.assertEqual(discounted_price(D('1379.95'), D('10')), D('1241.9550'))
        self.assertEqual(discounted_price(D('169.95'), D('25')), D('127.4625'))
        self.assertEqual(line_amount(D('4'), D('314.955')), D('1259.82'))
        self.assertEqual(line_amount(D('1'), D('1241.955')), D('1241.96'))
        self.assertEqual(unit_price(D('108.954045')), D('108.9540'))
        self.chair.price = D('349.95'); self.chair.save()
        doc = self.save([dict(item_id=self.chair.pk, quantity='4', price='349.95', discount_percent='10', discount_reason='Sale')],
                        [dict(method='cash', amount='1348.01')])
        line = doc.lines.get()
        self.assertEqual((line.price, line.original_price, line.discount_percent, line.amount), (D('314.9550'), D('349.95'), D('10'), D('1259.82')))
        self.assertEqual((doc.subtotal, doc.tax, doc.total), (D('1259.82'), D('88.19'), D('1348.01')))
        payload = document_data(doc)
        self.assertEqual((payload['lines'][0]['price'], payload['lines'][0]['display_price'], payload['lines'][0]['amount']), ('314.955', '314.96', '1259.82'))
        # Re-saving the unchanged line keeps the exact price; typing a whole-cent price clears the percent.
        saved = self.save(payload['lines'], payload['payments'], token=doc.token, version=doc.version)
        self.assertEqual((saved.lines.get().price, saved.total), (D('314.9550'), D('1348.01')))
        rows = [dict(payload['lines'][0], price='300', discount_percent='0')]
        saved = self.save(rows, [dict(method='cash', amount='1284')], token=saved.token, version=saved.version)
        self.assertEqual((saved.lines.get().price, saved.lines.get().discount_percent, saved.total), (D('300'), D('0'), D('1284')))

    def test_typed_fractional_cent_price_rejected(self):
        with self.assertRaisesMessage(WorkflowError, 'whole cents'):
            self.save([dict(item_id=self.chair.pk, quantity='1', price='314.955')], [dict(method='cash', amount='337')])
        # The register sends the derived 4-decimal price together with the percent: accepted, re-derived by the server.
        self.chair.price = D('349.95'); self.chair.save()
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='314.955', discount_percent='10', discount_reason='Sale')], [dict(method='cash', amount='337.01')])
        self.assertEqual((doc.lines.get().price, doc.subtotal, doc.tax, doc.total), (D('314.9550'), D('314.96'), D('22.05'), D('337.01')))  # tax on the exact 314.955

    def test_line_and_receipt_percent_together_tax_on_discounted_amounts(self):
        # Line 25 % off 169.95 -> 127.4625 (ext 127.46), then 5 % receipt discount: 6.37; tax 7 % on 121.09 = 8.48.
        self.chair.price = D('169.95'); self.chair.save()
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='169.95', discount_percent='25', discount_reason='Sale')],
                        [dict(method='cash', amount='129.57')], discount_percent='5', discount_reason='Sale')
        line = doc.lines.get()
        self.assertEqual((line.price, line.amount, line.receipt_discount, line.net_amount), (D('127.4625'), D('127.46'), D('6.37'), D('121.09')))
        self.assertEqual((doc.subtotal, doc.discount, doc.tax, doc.total), (D('127.46'), D('6.37'), D('8.48'), D('129.57')))
        self.assertEqual(tax_amount([(D('121.09'), D('7'))]), D('8.48'))

    def register_line_percent(self, line, steps):
        """Run the register's own applyLinePercent (the marked pure block, in Node) over `line` after each step —
        a dict of fields to set, as the inputs' handlers do (typing a price sets price_typed) — and return the line."""
        import json
        import subprocess
        from .test_qb_half_up import register_math, run_node
        script = (register_math() + '\nconst line=' + json.dumps(line) + ';const steps=' + json.dumps(steps) + ';'
                  'for(const step of steps){Object.assign(line,step);applyLinePercent(line);}console.log(JSON.stringify(line));')
        return json.loads(run_node(script))

    def test_line_percent_is_always_off_the_regular_price_on_both_sides(self):
        # "use the QuickBooks way": catalog $100, cashier types $80 and then 10 % off -> $90. The % is taken
        # off the item's regular price and replaces the typed price, in the register and on the server (all 1,103
        # line-% lines in the 10/9 data are regular x (1 - %)).
        picked = dict(item_id=self.chair.pk, quantity='1', price='100.00', original_price='100.00', tax_rate='7')  # addCatalogItem
        line = self.register_line_percent(picked, [dict(price='80', price_typed=True, discount_percent='0'), dict(discount_percent='10')])
        self.assertEqual((line['price'], line['original_price'], line['price_typed']), ('90.00', '100.00', False))
        doc = self.save([dict(line, discount_reason='Sale')], [dict(method='cash', amount='96.30')])
        saved = doc.lines.get()
        self.assertEqual((saved.price, saved.original_price, saved.discount_percent, saved.amount, doc.total), (D('90'), D('100'), D('10'), D('90'), D('96.30')))
        # Reopened later and the % changed: still off the $100 regular price.
        payload = document_data(doc)
        self.assertEqual(payload['lines'][0]['original_price'], '100.00')
        line = self.register_line_percent(payload['lines'][0], [dict(discount_percent='25')])
        self.assertEqual(line['price'], '75.00')
        saved = self.save([line], [dict(method='cash', amount='80.25')], token=doc.token, version=doc.version).lines.get()
        self.assertEqual((saved.price, saved.original_price), (D('75'), D('100')))
        # A crafted base (a typed price sent as the regular price) is refused, never saved as a different amount.
        with self.assertRaisesMessage(WorkflowError, 'regular price of'):
            self.save([dict(item_id=self.chair.pk, quantity='1', price='72', original_price='80.00', discount_percent='10', discount_reason='Sale')], [])
        # Catalog price changed between picking the item and saving: refused with a plain message, not a silent new amount.
        picked = dict(item_id=self.table.pk, quantity='1', price='150.00', original_price='150.00', tax_rate='7')
        line = self.register_line_percent(picked, [dict(discount_percent='10')])
        self.table.price = D('200'); self.table.save()
        with self.assertRaisesMessage(WorkflowError, 'is now $200.00'):
            self.save([dict(line, discount_reason='Sale')], [dict(method='cash', amount='144.45')])
        # The base is validated like any typed price (whole cents).
        with self.assertRaisesMessage(WorkflowError, 'regular price'):
            self.save([dict(item_id=self.chair.pk, quantity='1', price='72', original_price='80.123', discount_percent='10', discount_reason='Sale')], [])
        # Without a base from the client (API), the catalog price is the base.
        saved = self.save([dict(item_id=self.chair.pk, quantity='1', price='90', discount_percent='10', discount_reason='Sale')], [dict(method='cash', amount='96.30')]).lines.get()
        self.assertEqual((saved.price, saved.original_price), (D('90'), D('100')))
        # A misc line (no item) has no regular price: the price typed on it is the base.
        line = self.register_line_percent(dict(quantity='1', price='', tax_rate='7', item_name='MISC'), [dict(price='50', price_typed=True), dict(discount_percent='10')])
        self.assertEqual((line['price'], line['original_price']), ('45.00', '50.00'))

    def test_restocking_fee_is_charged_on_the_returned_net_amount(self):
        # Review finding 3: a $100 item sold with a 10 % receipt discount (paid 96.30) comes back with the
        # 20 % fee. The customer paid $90 for it, so the fee is $18 (not $20): refund 90 + 6.30 - 18 - 1.26 = 77.04.
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='100')], [dict(method='cash', amount='96.30')],
                        discount_percent='10', discount_reason='Sale')
        ret = self.save([self.returned(doc)], [dict(method='cash', amount='-77.04')], restocking_fee=True)
        fee = ret.lines.get(restocking_fee=True)
        returned = ret.lines.get(returned_from_line__isnull=False)
        self.assertEqual((returned.amount, returned.receipt_discount, returned.net_amount), (D('-100'), D('-10'), D('-90')))
        self.assertEqual((fee.price, fee.receipt_discount, fee.tax_rate), (D('18.00'), D('0'), D('7')))
        self.assertEqual((ret.subtotal, ret.discount, ret.tax, ret.total), (D('-82'), D('-10'), D('-5.04'), D('-77.04')))
        # The register figures the same fee from the same net amounts (restockingFees in the pure block).
        import json
        import subprocess
        from .test_qb_half_up import register_math, run_node
        page = document_data(ret)
        script = (register_math() + '\nconst lines=' + json.dumps(page['lines']) + ';'
                  'const fees=restockingFees(lines,"0","sale",20000);const t=qbTotals(lines,"0","sale",false,fees);'
                  'console.log(JSON.stringify({fees,sub:t.sub,discount:t.discount,tax:t.tax,total:t.total}));')
        got = json.loads(run_node(script))
        self.assertEqual(got, dict(fees=[dict(tax_rate='7', cents=1800)], sub=-8200, discount=-1000, tax=-504, total=-7704))
