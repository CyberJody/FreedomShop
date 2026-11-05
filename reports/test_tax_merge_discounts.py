# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax basis (/) on top of the QB money math: discounted receipts and discounted returns give the same
tax on the receipt, in the Sales Tax report and in Daily Totals, to the cent.

Through the real register save path: a receipt with a whole-cent line and a 4-decimal line (25 % line discount on
129.95 = 97.4625), a 10 % receipt discount allocated per line, tax on the unrounded discounted basis; the same receipt
part-charged to account and paid off later; and the discounted return of both lines.
"""
from datetime import timedelta
from decimal import Decimal as D

from reports import daily_totals, sales_tax
from sales.models import Document, Item
from sales.test_returns import SALE_DAY, ReturnTestCase, day

DAY2 = SALE_DAY + timedelta(days=1)
DAY3 = SALE_DAY + timedelta(days=2)


class DiscountedReceiptTaxTiesTests(ReturnTestCase):
    def setUp(self):
        super().setUp()
        self.vase = Item.objects.create(sku='6048', name='TEST VASE', description='TEST VASE', price='129.95', on_hand='4',
                                        tax_rate='7.000', average_cost='50.00')

    def discounted_sale(self, payments):
        # 100.00 + 129.95 × 75 % = 100.00 + 97.4625 → gross 197.46; 10 % receipt discount 19.75 (10.00 + 9.75);
        # tax on 90 + 87.7125 = 177.7125 × 7 % = 12.44; total 190.15.
        return self.save([dict(item_id=self.chair.pk, quantity='1', price='100'),
                          dict(item_id=self.vase.pk, quantity='1', price='129.95', original_price='129.95', discount_percent='25')],
                         payments, discount_percent='10', discount_reason='Sale')

    def dated(self, doc, when):
        Document.objects.filter(pk=doc.pk).update(completed_at=when)
        return Document.objects.get(pk=doc.pk)

    def reports(self, when):
        d = when.date()
        tax_report = sales_tax.build(day(d), detailed=True)
        rows = [r for s in tax_report.sections for r in s.rows]
        daily = daily_totals.build(day(d))
        return tax_report, rows, daily.totals

    def test_discounted_receipt_paid_in_full(self):
        sale = self.dated(self.discounted_sale([dict(method='cash', amount='190.15')]), SALE_DAY)
        self.assertEqual((sale.subtotal, sale.discount, sale.tax, sale.total), (D('197.46'), D('19.75'), D('12.44'), D('190.15')))
        self.assertEqual(sorted(line.receipt_discount for line in sale.lines.all()), [D('9.75'), D('10.00')])
        report, rows, daily = self.reports(SALE_DAY)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.gross, row.discount, row.taxable, row.nontaxable, row.tax, row.total),
                         (D('197.46'), D('19.75'), D('177.71'), D('0.00'), D('12.44'), D('190.15')))
        self.assertEqual((report.tax, daily['tax'], daily['received']), (sale.tax, sale.tax, D('190.15')))

    def test_discounted_receipt_part_charged_then_paid(self):
        sale = self.dated(self.discounted_sale([dict(method='cash', amount='100'), dict(method='account', amount='90.15')]), SALE_DAY)
        self.assertEqual(sale.tax, D('12.44'))
        report, rows, daily = self.reports(SALE_DAY)
        # Sale day: 12.44 × 100 ÷ 190.15 = 6.54 on the cash part; 5.90 waits on the account.
        self.assertEqual((rows[0].tax, rows[0].total, report.tax, daily['tax'], daily['received']),
                         (D('6.54'), D('100.00'), D('6.54'), D('6.54'), D('100.00')))
        self.assertEqual((report.charged_to_account, report.charged_to_account_tax), (D('90.15'), D('5.90')))
        payment = self.dated(self.save([dict(account_payment=True, quantity='1', price='90.15')], [dict(method='check', amount='90.15')]), DAY2)
        self.assertEqual(payment.lines.get().item_name, 'Payment On Account')
        report2, rows2, daily2 = self.reports(DAY2)
        self.assertEqual((rows2[0].kind, rows2[0].tax, report2.tax, daily2['tax']), ('account', D('5.90'), D('5.90'), D('5.90')))
        self.assertEqual(report.tax + report2.tax, sale.tax)

    def test_discounted_return_of_both_lines(self):
        sale = self.dated(self.discounted_sale([dict(method='cash', amount='190.15')]), SALE_DAY)
        chair_line, vase_line = sale.lines.order_by('position')
        ret = self.dated(self.save([self.returned(chair_line), self.returned(vase_line)], [dict(method='cash', amount='-190.15')]), DAY3)
        # The original receipt's 10 % is re-applied to the returned lines (QB's practice): same money back out.
        self.assertEqual((ret.subtotal, ret.discount, ret.tax, ret.total), (D('-197.46'), D('-19.75'), D('-12.44'), D('-190.15')))
        report, rows, daily = self.reports(DAY3)
        self.assertEqual((rows[0].gross, rows[0].discount, rows[0].taxable, rows[0].tax, rows[0].total),
                         (D('-197.46'), D('-19.75'), D('-177.71'), D('-12.44'), D('-190.15')))
        self.assertEqual((report.tax, daily['tax'], daily['received']), (ret.tax, ret.tax, D('-190.15')))
        # Over the three days the sale and its return net to nothing in both reports.
        self.assertEqual(sales_tax.build(_span(SALE_DAY.date(), DAY3.date())).tax, D('0.00'))
        self.assertEqual(daily_totals.build(_span(SALE_DAY.date(), DAY3.date())).totals['tax'], D('0.00'))


def _span(start, end):
    from reports import ny_periods
    return ny_periods.resolve('custom', '', start, start, end)
