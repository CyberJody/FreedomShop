# SPDX-License-Identifier: AGPL-3.0-or-later
"""General Sales and Sales by Vendor report what each line really sold for (Review finding 4): the gross
extension less the line's share of a receipt-level discount, negative on returns, in the rows, totals, margins and
exports. Documents come through the real register save path so the shares are the ones the app stores."""
from decimal import Decimal as D
from io import BytesIO

from openpyxl import load_workbook

from reports import exports, general_sales, sales_by_vendor, tabular_exports
from sales.models import Document
from sales.test_returns import SALE_DAY, ReturnTestCase, day


class NetLineAmountReportTests(ReturnTestCase):
    def setUp(self):
        super().setUp()
        from purchasing.models import Vendor
        vendor = Vendor.objects.create(company='TEST VENDOR', code='TV')
        self.chair.primary_vendor = vendor; self.chair.save()
        self.table.primary_vendor = vendor; self.table.save()
        # $100 chair + $150 table, 10 % receipt discount: shares 10 + 15; paid (250 - 25) x 1.07 = 240.75.
        self.sale = self.save([dict(item_id=self.chair.pk, quantity='1', price='100'), dict(item_id=self.table.pk, quantity='1', price='150')],
                              [dict(method='cash', amount='240.75')], discount_percent='10', discount_reason='Sale')
        # The chair comes back: the refund is its discounted price, 90 + 6.30.
        self.ret = self.save([self.returned(self.sale)], [dict(method='cash', amount='-96.30')])
        Document.objects.filter(pk__in=[self.sale.pk, self.ret.pk]).update(completed_at=SALE_DAY)
        self.period = day(SALE_DAY.date())

    def test_general_sales_amounts_and_margins_are_net_of_receipt_discount_shares(self):
        report = general_sales.build(self.period, optional_keys={'cost', 'margin'})
        rows = [r for g in report.groups for r in g.rows]
        self.assertEqual([(r['sku'], r['price'], r['amount'], r['cost'], r['margin'], r['discounted']) for r in rows],
                         [('6045', D('100'), D('90'), D('40'), D('50'), True), ('6046', D('150'), D('135'), D('60'), D('75'), True),
                          ('6045', D('100'), D('-90'), D('-40'), D('-50'), True)])
        self.assertEqual((report.totals['amount'], report.totals['cost'], report.totals['margin']), (D('135'), D('60'), D('75')))
        self.assertEqual(report.discounted_lines, 3)
        ws = load_workbook(BytesIO(tabular_exports.xlsx_bytes(report))).active
        cells = [[c.value for c in row] for row in ws.iter_rows()]
        grand = [r for r in cells if r and r[0] == 'Grand total'][0]
        self.assertEqual(D(str(grand[cells[5].index('Ext Price')])), D('135'))
        self.assertEqual(D(str(grand[cells[5].index('Margin')])), D('75'))

    def test_sales_by_vendor_detail_and_summaries_are_net_of_receipt_discount_shares(self):
        start = end = SALE_DAY.date()
        report = sales_by_vendor.build(start, end)
        rows = [r for g in report.groups for r in g.rows]
        self.assertEqual([(r.sku, r.price, r.amount, r.discounted) for r in rows],
                         [('6045', D('100'), D('90'), True), ('6046', D('150'), D('135'), True), ('6045', D('100'), D('-90'), True)])
        self.assertEqual((report.amount, report.groups[0].amount, report.discounted_lines), (D('135'), D('135'), 3))
        ws = load_workbook(BytesIO(exports.xlsx_bytes(report))).active
        cells = [[c.value for c in row] for row in ws.iter_rows()]
        grand = [r for r in cells if r and r[0] == 'Grand total'][0]
        self.assertEqual(D(str(grand[8])), D('135'))
        items = sales_by_vendor.summarize(start, end, view='items')
        by_name = {r['name']: r for g in items.groups for r in g.rows}
        self.assertEqual((by_name['TEST CHAIR']['amount'], by_name['TEST CHAIR']['cost'], by_name['TEST CHAIR']['margin_pct']), (D('0'), D('0'), None))
        self.assertEqual((by_name['TEST TABLE']['amount'], by_name['TEST TABLE']['cost'], by_name['TEST TABLE']['margin_pct']), (D('135'), D('60'), D('55.55')))
        vendors = sales_by_vendor.summarize(start, end, view='vendors')
        self.assertEqual((vendors.totals['amount'], vendors.totals['cost'], vendors.totals['margin_pct']), (D('135'), D('60'), D('55.55')))
