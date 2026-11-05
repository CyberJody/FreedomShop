# SPDX-License-Identifier: AGPL-3.0-or-later
"""a dollar discount on a register line (QB's Unit Discount), next to the line % off."""
import json
from decimal import Decimal as D
from unittest import mock

from .letter_printing import line_discount_text
from .models import Item
from .services import WorkflowError
from .test_qb_half_up import register_math, run_node
from .test_returns import ReturnTestCase
from .views import document_data


class LineDollarDiscountTests(ReturnTestCase):
    def setUp(self):
        super().setUp()
        self.dresser = Item.objects.create(sku='7001', name='TEST DRESSER', description='TEST DRESSER', price='484.95',
                                           on_hand='5', tax_rate='7.000', average_cost='200.00')

    def line(self, qty='1', off='35', **extra):
        return {**dict(item_id=self.dresser.pk, quantity=qty, price='484.95', original_price='484.95', discount_amount=off,
                       discount_percent='0', tax_rate='7'), **extra}

    def register(self, lines, pct='0'):
        """The register's own figures for these lines: apply each line's discount (applyLinePercent, as typing in the
        line's discount box does), then total them (qbTotals) - the same JS the sale screen runs."""
        script = (register_math() + '\nconst lines=' + json.dumps(lines) + ';'
                  'for(const l of lines){if(l.item_id!=null&&(Number(l.discount_amount)||Number(l.discount_percent)))applyLinePercent(l);}'
                  'const t=qbTotals(lines,' + json.dumps(pct) + ',"sale",false);'
                  'console.log(JSON.stringify({sub:t.sub,discount:t.discount,tax:t.tax,total:t.total,valid:t.valid,prices:lines.map(l=>l.price)}));')
        return json.loads(run_node(script))

    @staticmethod
    def cents(doc):
        return dict(sub=int(doc.subtotal * 100), discount=int(doc.discount * 100), tax=int(doc.tax * 100), total=int(doc.total * 100))

    def test_35_off_a_484_95_line_server_total_equals_register(self):
        doc = self.save([self.line(discount_reason='Sale')], [dict(method='cash', amount='481.45')])
        line = doc.lines.get()
        self.assertEqual((line.price, line.original_price, line.discount_percent, line.amount), (D('449.95'), D('484.95'), D('0'), D('449.95')))
        self.assertEqual(line.dollars_off, D('35.00'))
        self.assertEqual((doc.subtotal, doc.tax, doc.total), (D('449.95'), D('31.50'), D('481.45')))
        got = self.register([self.line()])
        self.assertEqual({k: got[k] for k in ('sub', 'discount', 'tax', 'total')}, self.cents(doc))
        self.assertEqual(got['prices'], ['449.95'])

    def test_quantity_two_takes_the_dollars_off_each_unit(self):
        # QB's Unit Discount: Ext Discount = qty x Unit Discount. $35 off each of 2 = $70 off the line.
        doc = self.save([self.line(qty='2')], [dict(method='cash', amount='962.89')])
        line = doc.lines.get()
        self.assertEqual((line.price, line.amount), (D('449.95'), D('899.90')))
        self.assertEqual((doc.subtotal, doc.tax, doc.total), (D('899.90'), D('62.99'), D('962.89')))
        got = self.register([self.line(qty='2')])
        self.assertEqual({k: got[k] for k in ('sub', 'discount', 'tax', 'total')}, self.cents(doc))

    def test_mixed_receipt_with_a_receipt_percent_discount(self):
        lines = [self.line(), dict(item_id=self.chair.pk, quantity='3', price='100.00', original_price='100.00', discount_percent='12.5', tax_rate='7'),
                 dict(item_id=self.table.pk, quantity='1', price='150.00', original_price='150.00', tax_rate='7')]
        got = self.register([dict(l) for l in lines], pct='5')
        doc = self.save(lines, [dict(method='cash', amount=str(D(got['total']) / 100))], discount_percent='5', discount_reason='Sale')
        self.assertEqual({k: got[k] for k in ('sub', 'discount', 'tax', 'total')}, self.cents(doc))
        self.assertEqual([l.price for l in doc.lines.all()], [D('449.95'), D('87.50'), D('150.00')])
        # 449.95 + 262.50 + 150 = 862.45; 5 % = 43.12; tax on 819.33 = 57.35; total 876.68
        self.assertEqual((doc.subtotal, doc.discount, doc.tax, doc.total), (D('862.45'), D('43.12'), D('57.35'), D('876.68')))

    def test_dollars_cannot_exceed_the_price_or_go_with_a_percent(self):
        with self.assertRaisesMessage(WorkflowError, 'The $485.00 discount on TEST DRESSER is more than its $484.95 price.'):
            self.save([self.line(off='485')], [dict(method='cash', amount='0')])
        with self.assertRaisesMessage(WorkflowError, 'percent or a dollar amount, not both'):
            self.save([self.line(discount_percent='10')], [dict(method='cash', amount='0')])
        got = self.register([self.line(off='485')])
        self.assertFalse(got['valid'])
        # The whole price off is allowed (QB gives items away this way: price 0).
        doc = self.save([self.line(off='484.95')], [])
        self.assertEqual((doc.lines.get().price, doc.total), (D('0.00'), D('0.00')))

    def test_reopened_line_shows_dollars_and_resaves_unchanged_with_reason(self):
        doc = self.save([self.line(discount_reason='Sale', discount_note='TEST floor model')], [dict(method='cash', amount='481.45')], action='hold')
        payload = document_data(doc)
        row = payload['lines'][0]
        self.assertEqual((row['discount_amount'], row['discount_percent'], row['original_price'], row['display_price']), ('35.00', '0', '484.95', '449.95'))
        again = self.save(payload['lines'], [dict(method='cash', amount='481.45')], token=doc.token, version=doc.version)
        line = again.lines.get()
        self.assertEqual((again.total, line.price, line.discount_reason, line.discount_note), (D('481.45'), D('449.95'), 'Sale', 'TEST floor model'))
        # A % line and an undiscounted line carry no dollar amount.
        plain_doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], [dict(method='cash', amount='107.00')])
        self.assertEqual(document_data(plain_doc)['lines'][0]['discount_amount'], '0')

    def test_receipt_and_return_printouts_show_the_dollar_discount_like_orders(self):
        from .printing import Receipt, render_receipt
        sale = self.save([self.line(discount_reason='Sale')], [dict(method='cash', amount='481.45')])
        line = sale.lines.get()
        self.assertEqual(line_discount_text(line), 'Discount: $35.00 off $484.95 - Sale')
        drawn = []
        original = Receipt.wrap
        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        with mock.patch.object(Receipt, 'wrap', spy):
            render_receipt(sale)
        self.assertIn('Discount: $35.00 off $484.95 - Sale', drawn)
        # The return refunds the discounted price and prints the discount it was sold with.
        ret = self.save([self.returned(sale)], [dict(method='cash', amount='-481.45')])
        self.assertEqual((ret.lines.get().price, ret.total), (D('449.95'), D('-481.45')))
        drawn.clear()
        with mock.patch.object(Receipt, 'wrap', spy):
            render_receipt(ret)
        self.assertIn('Discount: $35.00 off $484.95 - Sale', drawn)

    def test_line_percent_text_unchanged(self):
        doc = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00', original_price='100.00', discount_percent='10', discount_reason='Sale')],
                        [dict(method='cash', amount='96.30')])
        self.assertEqual(line_discount_text(doc.lines.get()), 'Discount: 10 % off $100.00 - Sale')
