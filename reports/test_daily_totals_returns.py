# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daily Totals' Gross sales / Returns split on returns from a %-discounted receipt (Review finding 5):
returned merchandise counts at what it sold for (net of the receipt discount share) with tax on that discounted
basis, so a $100 item from a 10 %-off receipt (paid 96.30) returns as 96.30, not 107. Full return, partial return
and exchange, all through the real register save path."""
from decimal import Decimal as D

from reports import daily_totals
from sales.models import Document
from sales.test_returns import SALE_DAY, ReturnTestCase, day


class DailyTotalsDiscountedReturnTests(ReturnTestCase):
    def summary(self, *docs):
        Document.objects.filter(pk__in=[d.pk for d in docs]).update(completed_at=SALE_DAY)
        report = daily_totals.build(day(SALE_DAY.date()))
        return {s['label'].split(' (')[0]: s['amount'] for s in report.summary}, report.totals

    def discounted_sale(self, qty='1'):
        total = str((D('100') * D(qty) * D('0.9') * D('1.07')).quantize(D('0.01')))
        return self.save([dict(item_id=self.chair.pk, quantity=qty, price='100')], [dict(method='cash', amount=total)],
                         discount_percent='10', discount_reason='Sale')

    def test_full_return(self):
        sale = self.discounted_sale()
        ret = self.save([self.returned(sale)], [dict(method='cash', amount='-96.30')])
        self.assertEqual(ret.total, D('-96.30'))
        summary, totals = self.summary(sale, ret)
        self.assertEqual((summary['Gross sales'], summary['Returns'], summary['Net sales'], summary['Refunds paid']),
                         (D('96.30'), D('-96.30'), D('0'), D('-96.30')))
        self.assertEqual((totals['received'], totals['tax']), (D('0'), D('0')))

    def test_partial_return(self):
        sale = self.discounted_sale('2')  # 192.60 paid
        ret = self.save([self.returned(sale)], [dict(method='cash', amount='-96.30')])
        summary, totals = self.summary(sale, ret)
        self.assertEqual((summary['Gross sales'], summary['Returns'], summary['Net sales']), (D('192.60'), D('-96.30'), D('96.30')))
        self.assertEqual((totals['received'], totals['tax']), (D('96.30'), D('6.30')))

    def test_exchange(self):
        sale = self.discounted_sale()
        # The chair (sold for 90 + 6.30) comes back and a $50 lamp goes out on the same receipt: refund 42.80.
        exchange = self.save([self.returned(sale), dict(item_id=self.lamp.pk, quantity='1', price='50')], [dict(method='cash', amount='-42.80')])
        self.assertEqual((exchange.subtotal, exchange.discount, exchange.tax, exchange.total), (D('-50'), D('-10'), D('-2.80'), D('-42.80')))
        summary, totals = self.summary(sale, exchange)
        self.assertEqual((summary['Gross sales'], summary['Returns'], summary['Net sales']), (D('149.80'), D('-96.30'), D('53.50')))
        self.assertEqual((totals['received'], totals['tax']), (D('53.50'), D('3.50')))
