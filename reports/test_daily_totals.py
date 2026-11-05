# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daily Totals cash-basis rules; legacy Sales Tax tests are deliberately unchanged."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from io import BytesIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register
from customers.models import Customer
from sales.models import Document, Line, Tender, OrderPayment, Printer, money
from . import ny_periods, sales_tax, sales_tax_exports
from .daily_totals import AMOUNTS, Ledger

NY = ZoneInfo('America/New_York')
WHEN = datetime(2026, 9, 24, 12, tzinfo=NY)


def period(start=date(2026, 9, 24), end=date(2026, 9, 24)):
    return ny_periods.resolve('custom', '', start, start, end)


class DailyTotalsTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='test', first_name='Daily')

    def doc(self, total='107', tax='7', payments=(('cash', '107'),), when=WHEN,
            imported=False, poa=False, order=False, **kwargs):
        obj = Document.objects.create(kind='order' if order else 'sale', status='open' if order else 'completed',
            customer=self.customer, customer_name='Daily', completed_at=None if order else when,
            subtotal=money(D(total) - D(tax)), total=money(total), tax=money(tax),
            payment_breakdown=[] if imported or order else [{'method': k, 'amount': v} for k, v in payments],
            source_id=f'import-{Document.objects.count()}' if imported else None, **kwargs)
        Document.objects.filter(pk=obj.pk).update(created_at=when)
        obj.created_at = when
        if imported:
            for pos, (method, amount) in enumerate(payments):
                Tender.objects.create(document=obj, position=pos, method=method, amount=money(amount))
        if poa:
            Line.objects.create(document=obj, position=1, item_name='Payment On Account', description='Payment',
                                quantity=1, price=money(total), tax_rate=0)
        return obj

    def test_all_columns_and_by_day_totals(self):
        cash = self.doc(imported=True, payments=(('cash', '120'), ('cash', '-13')), source_number=1)
        Tender.objects.create(document=cash, position=3, method='account', amount=999, use_in_total=True)
        charged = self.doc(total='107', tax='7', payments=(('debit', '50'), ('account', '57')))
        new = self.doc(total='214', tax='14', order=True, payments=(), when=WHEN - timedelta(hours=2))
        OrderPayment.objects.create(order=new, created_at=WHEN, amount=107, method='check')
        old = self.doc(total='1070', tax='70', order=True, payments=(), when=WHEN - timedelta(days=1))
        dep = self.doc(total='214', tax='0', imported=True, receipt_type=2, deposit_order=old,
                       payments=(('credit', '214'),))
        poa = self.doc(total='57', tax='0', payments=(('check', '57'),), poa=True, when=WHEN + timedelta(hours=1))
        exempt = self.doc(total='100', tax='0', payments=(('cash', '100'),), tax_location='Exempt')
        gift = self.doc(payments=(('gift', '107'),))
        ret = self.doc(total='-107', tax='-7', imported=True, payments=(('cash', '-107'),), receipt_type=1)
        empty = self.doc(total='321', tax='21', order=True, payments=())
        report = sales_tax.build_daily(period())
        rows = {r['number']: r for g in report.groups for r in g.rows}
        expected = {
            cash.numbered_label: ['107','107','0','7','107','0','0','0','0','0'],
            charged.numbered_label: ['50','107','57','3.27','0','0','50','57','0','0'],
            new.numbered_label: ['107','214','107','7','0','107','0','0','0','0'],
            old.numbered_label: ['214',None,None,'14','0','0','214','0','0','0'],
            poa.numbered_label: ['57','0','0','3.73','0','57','0','0','0','0'],
            exempt.numbered_label: ['100','100','0','0','100','0','0','0','0','100'],
            gift.numbered_label: ['0','107','0','7','0','0','0','0','107','0'],   # redeemed certificate: not money received, tax still counts
            ret.numbered_label: ['-107','-107','0','-7','-107','0','0','0','0','0'],
            empty.numbered_label: ['0','321','321','0','0','0','0','0','0','0'],
        }
        self.assertEqual(set(rows), set(expected))
        for label, values in expected.items():
            self.assertEqual([rows[label][k] for k in AMOUNTS], [None if v is None else D(v) for v in values], label)
            self.assertEqual(rows[label]['date'], date(2026, 9, 24))
            self.assertTrue(rows[label]['customer'].startswith('Daily'))
        self.assertIn('Payment on account', rows[poa.numbered_label]['customer'])
        for i, key in enumerate(AMOUNTS):
            expected_total = sum((D(values[i] or '0') for values in expected.values()), D(0))
            self.assertEqual(report.totals[key], expected_total, key)
            self.assertEqual(report.days[0][key], expected_total, key)
            self.assertEqual(report.groups[0].totals[key], expected_total, key)
        self.assertEqual(report.estimated, 0)

    def test_fifo_replays_history_and_releases_final_cent(self):
        self.doc(payments=(('credit', '50'), ('account', '57')), imported=True, when=WHEN - timedelta(days=3))
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('57'))   # QB: the $57 charge was still owed
        self.doc(total='100', tax='0', payments=(('account', '100'),), when=WHEN - timedelta(days=2))
        self.doc(total='10', tax='0', payments=(('cash', '10'),), poa=True, when=WHEN - timedelta(days=1))
        self.doc(total='147', tax='0', payments=(('cash', '147'),), poa=True)
        report = sales_tax.build_daily(period())
        self.assertEqual(report.totals['tax'], D('3.08')) # 3.73 deferred minus.65 collected before period
        self.assertEqual(report.estimated, 0)

    def test_fallback_and_exempt_and_no_future_charges(self):
        self.doc(total='100', tax='0', payments=(('cash', '100'),), poa=True)
        self.doc(payments=(('account', '107'),), when=WHEN + timedelta(days=1))
        report = sales_tax.build_daily(period())
        self.assertEqual(report.totals['tax'], D('6.54'))
        self.assertEqual(report.estimated, 1)
        self.assertTrue(any('1 payments on account estimated at 7/107' in n for n in report.notes))
        self.customer.tax_location = 'Exempt'
        self.customer.tax_exempt = True
        self.customer.save()
        self.assertEqual(sales_tax.build_daily(period()).totals['tax'], D('0'))

    def test_multiple_deposits_account_deposit_refund_and_day_end_balance(self):
        order = self.doc(order=True, total='1070', tax='70', when=WHEN - timedelta(hours=2))
        OrderPayment.objects.create(order=order, created_at=WHEN, amount=107, method='account')
        OrderPayment.objects.create(order=order, created_at=WHEN + timedelta(minutes=1), amount=214, method='check')
        OrderPayment.objects.create(order=order, created_at=WHEN + timedelta(minutes=2), amount=-107, method='check')
        OrderPayment.objects.create(order=order, created_at=WHEN + timedelta(days=1), amount=107, method='cash')
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=WHEN + timedelta(hours=1))
        report = sales_tax.build_daily(period())
        self.assertEqual(report.totals['new_sale'], D('1070'))
        self.assertEqual(report.totals['balance'], D('856'))
        self.assertEqual(report.totals['received'], D('214'))
        self.assertEqual(report.totals['account'], D('107'))
        self.assertEqual(report.totals['tax'], D('14'))
        self.assertEqual(report.estimated, 0)
        self.assertIsNone(report.groups[0].rows[1]['new_sale'])

    def test_cash_change_local_day_and_empty_days(self):
        self.doc(payments=(('cash', '120'),), tendered=D('120'), when=WHEN.replace(hour=23))
        report = sales_tax.build_daily(period(end=date(2026, 9, 25)))
        self.assertEqual(report.days[0]['received'], D('107'))
        self.assertEqual(report.days[1]['received'], D('0'))
        self.assertEqual(report.totals['tax'], D('7'))

    def test_unknown_deposit_and_exclusions_are_visible(self):
        self.doc(imported=True, receipt_type=2)
        self.doc(receipt_type=4)
        order = self.doc(order=True, when=WHEN-timedelta(days=1), deposit_used=D('107'))
        self.doc(imported=True, receipt_type=2, deposit_order=order)
        self.doc(source_order=order)
        cancelled = self.doc(order=True)
        Document.objects.filter(pk=cancelled.pk).update(status='cancelled')
        report = sales_tax.build_daily(period())
        self.assertEqual(report.lines, 1)
        self.assertEqual(report.totals['received'], D('107'))
        self.assertEqual(report.totals['tax'], D('0'))
        for text in ('unlinked deposits', 'payout receipts', 'app pickup', 'legacy deposits'):
            self.assertTrue(any(text in n for n in report.notes), text)

    def test_account_credit_and_payment_refund(self):
        from reports.tax_ledger import Lot, DEFAULT_LOCATION
        lot = lambda amount, tax, key=None: Lot(D(amount), D(tax), D(amount) - D(tax), DEFAULT_LOCATION, key)   # noqa: E731
        released = lambda allocations: (sum((a.tax for a in allocations), D('0')), sum((a.amount for a in allocations if a.estimated), D('0')))   # noqa: E731
        ledger = Ledger()
        ledger.charge(lot('107', '7', ('sale', 1)))
        self.assertEqual(released(ledger.pay(D('107'), False)), (D('7'), D('0')))
        self.assertEqual(released(ledger.pay(D('-53.50'), False)), (D('-3.50'), D('0')))
        self.assertEqual(released(ledger.pay(D('53.50'), False)), (D('3.50'), D('0')))
        ledger.charge(lot('-107', '-7'))
        ledger.charge(lot('214', '14', ('sale', 2)))
        self.assertEqual(released(ledger.pay(D('107'), False)), (D('7'), D('0')))

    def test_customer_isolation_partial_fallback_and_exempt_charge(self):
        self.doc(total='100', tax='0', payments=(('account', '100'),),
                 tax_location='Exempt', when=WHEN - timedelta(days=1))
        self.doc(total='207', tax='0', payments=(('cash', '207'),), poa=True)
        other = Customer.objects.create(source_id='other', first_name='Other')
        foreign = self.doc(total='1070', tax='70', payments=(('account', '1070'),),
                           when=WHEN-timedelta(days=2))
        Document.objects.filter(pk=foreign.pk).update(customer=other)
        report = sales_tax.build_daily(period())
        self.assertEqual(report.totals['tax'], D('7'))  # first $100 is known exempt; $107 unmatched
        self.assertEqual(report.estimated, 1)

    def test_imported_account_deposit_is_deferred_until_paid(self):
        order = self.doc(order=True, total='214', tax='14', when=WHEN-timedelta(days=2))
        self.doc(imported=True, total='107', tax='0', receipt_type=2, deposit_order=order,
                 payments=(('account', '107'),), when=WHEN-timedelta(days=1))
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('107'))   # QB: the deposit was still owed
        self.doc(total='107', tax='0', payments=(('check', '107'),), poa=True)
        report = sales_tax.build_daily(period())
        self.assertEqual(report.totals['tax'], D('7'))
        self.assertEqual(report.estimated, 0)

    def mixed_lines(self, doc, merchandise, poa):
        for pos, (name, amount) in enumerate(merchandise + [('Payment On Account', poa)], 1):
            Line.objects.create(document=doc, position=pos, item_name=name, source_id=f'mixed-{doc.pk}-{pos}',
                                quantity=-1 if D(amount) < 0 else 1, price=abs(money(amount)),
                                tax_rate=0 if name == 'Payment On Account' else 7)

    def test_qb_121052_return_credit_reduces_deferred_tax(self):
        self.doc(total='508.20', tax='33.24', payments=(('account', '508.20'),),
                 when=WHEN - timedelta(days=1))
        returned = self.doc(total='0', tax='-16.62', payments=(), imported=True, source_number=121052)
        self.mixed_lines(returned, [('NOI-MISC', '-237.48')], '254.10')
        Tender.objects.create(document=returned, position=1, method='account', amount=D('-254.10'), use_in_total=True)
        paid = self.doc(total='254.10', tax='0', payments=(('cash', '254.10'),), poa=True,
                        when=WHEN + timedelta(hours=1))
        report = sales_tax.build_daily(period())
        rows = {r['number']: r for g in report.groups for r in g.rows}
        self.assertEqual((rows[returned.numbered_label]['new_sale'], rows[returned.numbered_label]['tax']),
                         (D('-254.10'), D('0')))
        self.assertEqual(rows[returned.numbered_label]['received'], D('0'))
        self.assertEqual(rows[paid.numbered_label]['tax'], D('16.62'))
        self.assertEqual(report.estimated, 0)

    def test_qb_117971_mixed_paid_receipt_pays_oldest_charge(self):
        # Receipt amounts/lines/memo tender copied from QB; controlled older
        # charges make the FIFO allocation distinguishable from flat 7/107.
        self.doc(total='1000', tax='50', payments=(('account', '1000'),), when=WHEN-timedelta(days=2))
        self.doc(total='200', tax='0', payments=(('account', '200'),), when=WHEN-timedelta(days=1))
        paid = self.doc(total='802', tax='0', payments=(('cash', '802'),), imported=True, source_number=117971)
        self.mixed_lines(paid, [('OOOOOO', '0')], '802')
        Tender.objects.create(document=paid, position=2, method='account', amount=D('-802'), use_in_total=True)
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-802'))   # QB saw only the payment
        report = sales_tax.build_daily(period())
        row = report.groups[0].rows[0]
        self.assertEqual((row['new_sale'], row['tax'], row['received']), (D('0'), D('40.10'), D('802')))
        self.assertEqual(report.estimated, 0)

    def test_qb_116213_mixed_refund_splits_cash_and_account_credit(self):
        self.doc(total='1947.24', tax='127.39', payments=(('credit', '486.81'), ('account', '1460.43')),
                 imported=True, source_number=116191, when=WHEN-timedelta(days=3))
        returned = self.doc(total='-486.81', tax='-127.39', payments=(('credit', '-486.81'),),
                            imported=True, source_number=116213)
        self.mixed_lines(returned, [('HOS-CDG-5-WW/BRN', '-979.95'), ('HOS-CFA-45-WW/BRN', '-839.90')], '1460.43')
        Tender.objects.create(document=returned, position=2, method='account', amount=D('-1460.43'), use_in_total=True)
        report = sales_tax.build_daily(period())
        row = report.groups[0].rows[0]
        self.assertEqual((row['new_sale'], row['tax']), (D('-1947.24'), D('-31.85')))
        self.assertEqual(report.estimated, 0)
        combined = sales_tax.build_daily(period(start=date(2026, 9, 21)))
        self.assertEqual(combined.totals['received'], D('0'))
        self.assertEqual(combined.totals['tax'], D('0'))

    def test_mixed_sale_uses_recorded_poa_amount_and_adds_payment_tax(self):
        self.doc(total='107', tax='7', payments=(('account', '107'),), when=WHEN-timedelta(days=1))
        mixed = self.doc(total='160.51', tax='7', payments=(('cash', '160.51'),))
        self.mixed_lines(mixed, [('Merchandise', '100')], '53.50')
        Line.objects.filter(document=mixed, item_name='Payment On Account').update(recorded_amount=D('53.51'))
        report = sales_tax.build_daily(period())
        row = report.groups[0].rows[0]
        self.assertEqual((row['new_sale'], row['tax']), (D('107'), D('10.50')))
        self.assertEqual(report.estimated, 0)

    def test_net_paid_return_applies_only_cash_portion_as_payment(self):
        self.doc(total='321', tax='21', payments=(('account', '321'),), when=WHEN-timedelta(days=1))
        mixed = self.doc(total='107', tax='-7', payments=(('cash', '107'),))
        self.mixed_lines(mixed, [('Merchandise', '-100')], '214')
        report = sales_tax.build_daily(period())
        row = report.groups[0].rows[0]
        self.assertEqual((row['new_sale'], row['tax']), (D('-107'), D('7')))
        self.assertEqual(report.estimated, 0)

    def test_credit_carries_return_tax_across_different_rate_lots(self):
        from reports.tax_ledger import Lot, DEFAULT_LOCATION
        lot = lambda amount, tax, key=None: Lot(D(amount), D(tax), D(amount) - D(tax), DEFAULT_LOCATION, key)   # noqa: E731
        released = lambda allocations: (sum((a.tax for a in allocations), D('0')), sum((a.amount for a in allocations if a.estimated), D('0')))   # noqa: E731
        ledger = Ledger()
        ledger.charge(lot('100', '0', ('sale', 1)))
        ledger.charge(lot('214', '14', ('sale', 2)))
        # A $107 return credit (tax 7) funds the no-tax charge and $7 of the taxed one: its $7 is trued up to what it
        # bought that day (−6.54 on the $100, the 7 ¢ share on the $7 matches), the rest of charge 2 keeps its tax.
        self.assertEqual(sum((a.tax for a in ledger.charge(lot('-107', '-7'))), D('0')), D('-6.54'))
        self.assertEqual(released(ledger.pay(D('207'), False)), (D('13.54'), D('0')))     # lifetime: −6.54 + 13.54 = 7
        self.assertEqual(ledger.charge(lot('-107', '-7')), [])                               # nothing to fund yet
        self.assertEqual(sum((a.tax for a in ledger.charge(lot('207', '7', ('sale', 3)))), D('0')), D('-3.38'))   # 3.62 funded − 7 reversed
        self.assertEqual(released(ledger.pay(D('100'), False)), (D('3.38'), D('0')))      # lifetime: −3.38 + 3.38 = 0
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))

    def test_charge_paid_next_period_equals_combined_run(self):
        self.doc(total='107', tax='7', payments=(('account', '107'),),
                 when=WHEN.replace(month=8, day=31))
        self.doc(total='107', tax='0', payments=(('check', '107'),), poa=True)
        august = sales_tax.build_daily(period(date(2026, 8, 1), date(2026, 8, 31)))
        september = sales_tax.build_daily(period(date(2026, 9, 1), date(2026, 9, 30)))
        combined = sales_tax.build_daily(period(date(2026, 8, 1), date(2026, 9, 30)))
        for key in AMOUNTS:
            self.assertEqual(august.totals[key] + september.totals[key], combined.totals[key], key)
        self.assertEqual((august.totals['tax'], september.totals['tax']), (D('0'), D('7')))

    def test_overpayment_then_later_charge_is_funded_by_the_prepayment(self):
        self.doc(total='107', tax='7', payments=(('account', '107'),), when=WHEN-timedelta(days=1))
        self.doc(total='214', tax='0', payments=(('cash', '214'),), poa=True)
        # The extra $107 (estimated at 7/107 when received, flagged) is held as credit: the exempt charge the next
        # day is funded by it and the estimate is trued up (−7 that day); the $107 paid after that is a new
        # prepayment, estimated again. Each period's figures are fixed once written (review fix, finding 1).
        self.doc(total='107', tax='0', payments=(('account', '107'),),
                 tax_location='Exempt', when=WHEN+timedelta(days=1))
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=WHEN+timedelta(days=2))
        first = sales_tax.build_daily(period())
        funded = sales_tax.build_daily(period(date(2026, 9, 25), date(2026, 9, 25)))
        later = sales_tax.build_daily(period(date(2026,9,25), date(2026,9,26)))
        combined = sales_tax.build_daily(period(end=date(2026,9,26)))
        self.assertEqual((first.totals['tax'], first.estimated), (D('14'), 1))
        self.assertEqual((funded.totals['tax'], funded.totals['received'], funded.walk.reconciled_tax), (D('-7'), D('0'), D('-7')))
        self.assertEqual((later.totals['tax'], later.estimated), (D('0'), 1))
        for key in AMOUNTS:
            self.assertEqual(first.totals[key] + later.totals[key], combined.totals[key], key)

    def test_gift_and_new_sale_by_day_screen_excel_pdf(self):
        from openpyxl import load_workbook
        from reportlab.platypus import Table
        self.doc(payments=(('gift', '107'),))
        report = sales_tax.build_daily(period())
        self.assertIn('gift', [c.key for c in report.day_columns])
        self.assertIn('new_sale', [c.key for c in report.day_columns])
        for row in report.days:
            self.assertEqual(sum(row[k] for k in ('cash','check','card')), row['received'])
        self.assertEqual((report.totals['received'], report.totals['gift'], report.totals['tax']), (D('0'), D('107'), D('7')))
        self.assertTrue(any("Gift/Other = gift certificate" in n and 'not counted in Received' in n for n in report.notes))
        wb = load_workbook(BytesIO(sales_tax_exports.xlsx_bytes(report)))
        rows = list(wb['By day'].values)
        header = next(i for i, r in enumerate(rows) if 'Gift/Other' in r)
        cols = rows[header]
        data_row = next(r for r in rows[header+1:] if isinstance(r[0], datetime))
        self.assertEqual(data_row[cols.index('Gift/Other')], 107)
        self.assertEqual(data_row[cols.index('New Sale')], 107)
        with mock.patch('reportlab.platypus.Table', wraps=Table) as tables:
            pdf = sales_tax_exports.pdf_bytes(report)
        for column, paragraph in zip(report.day_columns, tables.call_args_list[0].args[0][0]):
            self.assertEqual(paragraph.style.alignment, 2 if column.kind == 'money' else 0)
        import pypdfium2
        parsed = pypdfium2.PdfDocument(pdf)
        text = parsed[0].get_textpage().get_text_range()
        self.assertIn('Gift/Other', text)
        self.assertIn('New Sale', text)
        parsed.close()
        from django.template.loader import render_to_string
        html = render_to_string('reports/daily-totals-page.html', {'report':report, 'period':report.period})
        by_day_html = html.split('</table>', 1)[0]
        self.assertIn('Gift/Other', by_day_html)
        self.assertIn('New Sale', by_day_html)

    def test_native_payment_on_imported_order_counts_like_sales_tax(self):
        # an app payment on an OPEN order from QuickBooks is money received, counted on its day by
        # both reports (before such orders were read-only and the payment was ignored).
        order = self.doc(total='214', tax='14', order=True, imported=True, when=WHEN-timedelta(days=1))
        OrderPayment.objects.create(order=order, created_at=WHEN, amount=107, method='cash')
        daily = sales_tax.build_daily(period())
        self.assertEqual((daily.totals['received'], daily.totals['tax']), (D('107'), D('7')))
        report = sales_tax.build(period(), True)
        self.assertEqual((report.total, report.tax), (D('107'), D('7')))

    def test_missing_tender_is_not_fabricated(self):
        # One undecodable amount beside a known total is arithmetic (total − the others), and said so in the notes.
        doc = self.doc(imported=True, payments=(('credit', '50'), ('cash', '57')))
        Tender.objects.filter(document=doc, method='credit').update(amount=None)
        report = sales_tax.build_daily(period())
        self.assertEqual((report.totals['received'], report.totals['card'], report.totals['tax']), (D('107'), D('50'), D('7')))
        self.assertTrue(any('could not decode' in note for note in report.notes))
        # Two unknown amounts stay unknown: nothing is invented.
        Tender.objects.filter(document=doc).update(amount=None)
        report = sales_tax.build_daily(period())
        self.assertEqual(report.totals['received'], D('0'))
        self.assertEqual(report.totals['tax'], D('0'))
        self.assertTrue(any('missing tender' in note for note in report.notes))

    def test_exports_include_both_tables_and_notes(self):
        from openpyxl import load_workbook
        self.doc()
        order = self.doc(order=True, when=WHEN - timedelta(days=1))
        OrderPayment.objects.create(order=order, amount=50, method='credit', created_at=WHEN)
        report = sales_tax.build_daily(period())
        data = sales_tax_exports.xlsx_bytes(report)
        wb = load_workbook(BytesIO(data))
        self.assertEqual(wb.sheetnames, ['By day', 'Documents'])
        values = list(wb['Documents'].values)
        self.assertTrue(any('Spreadsheet check:' in str(r[0]) for r in values))
        self.assertEqual(values[5][0:4], ('Date', 'Number', 'Customer', 'Received'))
        pdf = sales_tax_exports.pdf_bytes(report)
        self.assertTrue(pdf.startswith(b'%PDF'))
        import pypdfium2
        parsed = pypdfium2.PdfDocument(pdf)
        text = '\n'.join(page.get_textpage().get_text_range() for page in parsed)
        for word in ('Tax in', 'Received', 'Documents', 'Spreadsheet check:', order.numbered_label):
            self.assertIn(word, text)


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class DailyViewTests(TestCase):
    def setUp(self):
        sign_in_admin(self.client)

    def test_daily_periods_exports_and_mocked_print(self):
        Document.objects.create(kind='sale', status='completed', completed_at=WHEN, total=107, tax=7,
                                payment_breakdown=[{'method':'cash','amount':'107'}])
        for kind, data in [('today', {}), ('yesterday', {}), ('month', {'month':'2026-09'}),
                           ('quarter', {'quarter':'2027-3'}), ('year', {'year':'2027'}),
                           ('custom', {'start':'2026-09-24','end':'2026-09-24'})]:
            with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 9, 24)):
                response = self.client.get('/reports/daily-totals/', {'period':kind,'run':'1', **data})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['report'].daily)
            self.assertEqual(response.context['period'].kind, kind)
            self.assertNotContains(response, 'name="detail"')
            self.assertContains(response, 'data-print-url="/reports/daily-totals/print/"')
        query = {'period':'custom','start':'2026-09-24','end':'2026-09-24','run':'1'}
        response = self.client.get('/reports/daily-totals/', query)
        self.assertContains(response, 'Daily Totals · 9/24/2026')
        self.assertContains(response, '1 document row')
        for fmt in ('pdf','xlsx'):
            response = self.client.get('/reports/daily-totals/', {**query, 'format':fmt})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Disposition'], f'attachment; filename="Daily-Totals-2026-09-24-2026-09-24.{fmt}"')
        printer = Printer.objects.create(name='Test only', host='never-used', kind='letter')
        with mock.patch('reports.direct_print.print_report', return_value='Mock only') as send:
            response = self.client.post('/reports/daily-totals/print/', {**query, 'printer':printer.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(send.call_args.args[2], 'Daily Totals 9/24/2026 – 9/24/2026')
            self.assertTrue(send.call_args.args[1].startswith(b'%PDF'))
        self.assertEqual(self.client.session['report_printer'], str(printer.pk))
        self.assertEqual(self.client.get('/reports/daily-totals/', query).context['selected_printer'], str(printer.pk))

    def test_default_today_and_reset(self):
        with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 9, 24)):
            response = self.client.get('/reports/daily-totals/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['period'].kind, 'today')
            self.assertContains(response, 'value="today" selected')
            self.assertContains(response, 'Today · 9/24/2026')
            self.assertNotContains(response, 'name="detail"')
            self.assertContains(response, 'href="/reports/daily-totals/">Reset')
            response = self.client.get('/reports/daily-totals/', {'run':'1'})
            self.assertContains(response, 'Daily Totals · Today · 9/24/2026 · 0 document rows')

    def test_sales_tax_daily_falls_back_to_summary(self):
        response = self.client.get('/reports/sales-tax/', {'detail':'daily', 'run':'1'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['detailed'])
        self.assertFalse(getattr(response.context['report'], 'daily', False))
        select = response.content.decode().split('<select name="detail">')[1].split('</select>')[0]
        self.assertEqual(select.count('<option '), 2)
        self.assertIn('value="summary" selected', select)
        self.assertIn('value="detail"', select)
        self.assertNotIn('Daily Totals', select)
        self.assertNotContains(response, 'By day')

    def test_menus_and_admin_mode(self):
        self.assertContains(self.client.get('/reports/'), 'href="/reports/daily-totals/">Daily Totals')
        response = self.client.get('/')
        self.assertIn({'label':'Daily Totals', 'url':'/reports/daily-totals/', 'icon':'reports'}, response.context['report_tasks'])
        sign_in_register(self.client)   # the shared register login is not an admin
        self.assertEqual(self.client.get('/reports/daily-totals/').status_code, 302)
        self.assertEqual(self.client.post('/reports/daily-totals/print/').status_code, 403)

    def test_print_errors_and_export_validation(self):
        from sales.printing import PrintError
        query = {'period':'custom','start':'2026-09-24','end':'2026-09-24'}
        self.assertEqual(self.client.get('/reports/daily-totals/', {**query, 'format':'csv'}).status_code, 404)
        self.assertEqual(self.client.get('/reports/daily-totals/print/').status_code, 405)
        self.assertEqual(self.client.post('/reports/daily-totals/print/', query).status_code, 400)
        printer = Printer.objects.create(name='Test only', host='never-used', kind='letter')
        with mock.patch('reports.direct_print.print_report') as send:
            self.assertEqual(self.client.post('/reports/daily-totals/print/', {**query, 'printer':printer.pk}).status_code, 400)
            send.assert_not_called()
        Document.objects.create(kind='sale', status='completed', completed_at=WHEN, total=107, tax=7,
                                payment_breakdown=[{'method':'cash','amount':'107'}])
        with mock.patch('reports.direct_print.print_report', side_effect=PrintError('Mock failure')):
            response = self.client.post('/reports/daily-totals/print/', {**query, 'printer':printer.pk})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()['error'], 'Mock failure')
