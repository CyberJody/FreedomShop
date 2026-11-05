# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal as D
from sales.test_returns import ReturnTestCase, SALE_DAY, day
from reports import daily_totals


class ReturnSummaryTests(ReturnTestCase):
    def test_exchange_and_refund_split_reconcile_existing_figures(self):
        sale = self.sell('2')
        exchange = self.save([self.returned(sale), dict(item_id=self.table.pk, quantity='1', price='150')], [dict(method='cash', amount='53.50')])
        refund = self.save([self.returned(sale)], [dict(method='cash', amount='-107')])
        from sales.models import Document
        Document.objects.filter(pk__in=[exchange.pk, refund.pk]).update(completed_at=SALE_DAY)
        report = daily_totals.build(day(SALE_DAY.date()))
        gross, returns, net, refunds = [r['amount'] for r in report.summary]
        self.assertEqual(returns, D('-214'))
        self.assertEqual(gross, D('374.50'))
        self.assertEqual(net, report.totals['new_sale'])
        self.assertEqual(gross + returns, net)
        self.assertEqual(refunds, D('-107'))
        self.assertEqual(report.totals['received'], net)

    def test_account_credit_is_return_not_cash_refund(self):
        sale = self.sell('1')
        ret = self.save([self.returned(sale)], [dict(method='account', amount='-107')])
        from sales.models import Document
        Document.objects.filter(pk=ret.pk).update(completed_at=SALE_DAY)
        report = daily_totals.build(day(SALE_DAY.date()))
        self.assertEqual([r['amount'] for r in report.summary], [D('107'), D('-107'), D('0'), D('0')])
