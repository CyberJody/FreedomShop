# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sales Tax report: NYS periods, per-location math with whole cents, exports, direct print."""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from sales.models import Document, Item, Line, Printer, Tender

from . import ny_periods, sales_tax, sales_tax_exports
from .tests import AdminModeClientTests

NY = ZoneInfo('America/New_York')


class NYPeriodTests(TestCase):
    def test_tax_year_runs_march_to_february(self):
        self.assertEqual(ny_periods.tax_year_of(date(2026, 2, 28)), 2026)
        self.assertEqual(ny_periods.tax_year_of(date(2026, 3, 1)), 2027)
        self.assertEqual(ny_periods.tax_year_range(2026), (date(2025, 3, 1), date(2026, 2, 28)))
        self.assertEqual(ny_periods.tax_year_range(2028), (date(2027, 3, 1), date(2028, 2, 29)))   # leap year

    def test_quarters(self):
        self.assertEqual(ny_periods.quarter_range(2027, 1), (date(2026, 3, 1), date(2026, 5, 31)))
        self.assertEqual(ny_periods.quarter_range(2027, 2), (date(2026, 6, 1), date(2026, 8, 31)))
        self.assertEqual(ny_periods.quarter_range(2027, 3), (date(2026, 9, 1), date(2026, 11, 30)))
        self.assertEqual(ny_periods.quarter_range(2027, 4), (date(2026, 12, 1), date(2027, 2, 28)))
        self.assertEqual(ny_periods.quarter_range(2028, 4), (date(2027, 12, 1), date(2028, 2, 29)))
        for day, expected in [(date(2026, 3, 1), (2027, 1)), (date(2026, 5, 31), (2027, 1)), (date(2026, 6, 1), (2027, 2)),
                              (date(2026, 9, 30), (2027, 3)), (date(2026, 12, 1), (2027, 4)), (date(2027, 1, 15), (2027, 4)),
                              (date(2027, 2, 28), (2027, 4))]:
            self.assertEqual(ny_periods.quarter_of(day), expected, day)

    def test_choices_and_resolve(self):
        today = date(2026, 9, 30)
        self.assertEqual(ny_periods.month_choices(today)[:2], [('2026-09', 'September 2026'), ('2026-08', 'August 2026')])
        self.assertEqual(ny_periods.quarter_choices(today)[0][0], '2027-3')
        self.assertEqual(ny_periods.quarter_choices(today)[1][0], '2027-2')
        self.assertEqual(ny_periods.year_choices(today)[0][0], '2027')
        p = ny_periods.resolve('quarter', '2027-2', today)
        self.assertEqual((p.start, p.end), (date(2026, 6, 1), date(2026, 8, 31)))
        self.assertIn('Q2 2027', p.label)
        p = ny_periods.resolve('year', '2026', today)
        self.assertEqual((p.start, p.end), (date(2025, 3, 1), date(2026, 2, 28)))
        p = ny_periods.resolve('custom', '', today, date(2026, 9, 10), date(2026, 9, 1))
        self.assertEqual((p.start, p.end), (date(2026, 9, 1), date(2026, 9, 10)))   # swapped
        p = ny_periods.resolve('month', 'garbage', today)
        self.assertEqual((p.start, p.end), (date(2026, 9, 1), date(2026, 9, 30)))   # fallback
        p = ny_periods.resolve('quarter', '2027-9', today)
        self.assertEqual(p.start, date(2026, 9, 1))   # bad quarter -> current quarter
        p = ny_periods.resolve('custom', '', today, None, None)
        self.assertEqual(p.kind, 'month')   # missing custom dates -> current month

    def test_today_and_yesterday(self):
        p = ny_periods.resolve('today', '', date(2026, 10, 10))
        self.assertEqual((p.kind, p.start, p.end, p.label), ('today', date(2026, 10, 10), date(2026, 10, 10), 'Today · 10/10/2026'))
        p = ny_periods.resolve('yesterday', '', date(2026, 10, 10))
        self.assertEqual((p.kind, p.start, p.end, p.label), ('yesterday', date(2026, 10, 9), date(2026, 10, 9), 'Yesterday · 10/9/2026'))
        p = ny_periods.resolve('yesterday', 'garbage', date(2026, 3, 1), date(2020, 1, 1), None)   # stray input ignored; crosses month
        self.assertEqual((p.start, p.end), (date(2026, 2, 28), date(2026, 2, 28)))


def receipt(number, when, location, subtotal, tax, total, discount='0', receipt_type=0, customer='CUSTOMER', tenders=None, **extra):
    """An imported QB receipt. Paid in full by card unless `tenders` (list of (method, amount)) says otherwise —
    the cash-basis report counts only money received, so a receipt with no tender rows counts nothing."""
    doc = Document.objects.create(kind='sale', status='completed', source_id=f'R{number}', source_number=number, receipt_type=receipt_type,
                                  customer_name=customer, completed_at=datetime(*when, 14, 0, tzinfo=NY), tax_location=location,
                                  tax_exempt=location == 'Exempt', subtotal=Decimal(subtotal), discount=Decimal(discount), tax=Decimal(tax), total=Decimal(total), **extra)
    if tenders is None:
        tenders = [('credit', total)] if Decimal(total) else []
    for position, (method, amount) in enumerate(tenders, start=1):
        Tender.objects.create(document=doc, position=position, method=method, amount=Decimal(amount))
    return doc


def line(doc, position, qty, price, rate, item=None, name='Item'):
    return Line.objects.create(document=doc, position=position, item=item, sku=item.sku if item else '', item_name=item.name if item else name,
                               description=name, quantity=Decimal(qty), price=Decimal(price), tax_rate=Decimal(rate), source_id=f'{doc.pk}-{position}')


class SalesTaxReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.item = Item.objects.create(sku='X-1', name='Chair', description='Chair', price=Decimal('100'))
        # Plain taxable sale: 100 + 7 = 107
        r = receipt(1, (2026, 9, 2), 'Springfield', '100.00', '7.00', '107.00')
        line(r, 1, '1', '100', '7', cls.item)
        # Taxable with a non-taxable finish line: 200 taxable + 50 non-taxable, tax 14
        r = receipt(2, (2026, 9, 3), 'Springfield', '250.00', '14.00', '264.00')
        line(r, 1, '2', '100', '7', cls.item)
        line(r, 2, '1', '50', '0', name='PREMIUM FINISH')
        # Receipt-level 50% discount before tax: 1154.90 - 577.45 = 577.45 taxable, tax 40.42, total 617.87
        r = receipt(3, (2026, 9, 5), 'Springfield', '1154.90', '40.42', '617.87', discount='577.45')
        line(r, 1, '1', '889.95', '7', cls.item)
        line(r, 2, '1', '264.95', '7', cls.item)
        # Sale with a payment on account on the same receipt: item 100 + POA 300; QB subtotal includes the POA
        r = receipt(4, (2026, 9, 8), 'Springfield', '400.00', '7.00', '407.00')
        line(r, 1, '1', '100', '7', cls.item)
        line(r, 2, '1', '300', '0', name='Payment On Account')
        # Fractional-cent unit price: 1377.4525 -> 1377.45
        r = receipt(5, (2026, 9, 9), 'Springfield', '1377.45', '96.42', '1473.87')
        line(r, 1, '1', '1377.4525', '7', cls.item)
        # Reversal (return)
        r = receipt(6, (2026, 9, 10), 'Springfield', '-100.00', '-7.00', '-107.00', receipt_type=1)
        line(r, 1, '-1', '100', '7', cls.item)
        # Exempt location, with a receipt discount
        r = receipt(7, (2026, 9, 12), 'Exempt', '3074.85', '0', '2921.11', discount='153.74', customer='DEMO MUSEUM')
        line(r, 1, '1', '3074.85', '0', cls.item)
        # Deposit receipt (no lines) and an out-of-range receipt
        receipt(8, (2026, 9, 15), 'Springfield', '0', '0', '500.00', receipt_type=2)
        r = receipt(9, (2026, 10, 1), 'Springfield', '100.00', '7.00', '107.00')
        line(r, 1, '1', '100', '7', cls.item)
        # Held receipt is never a sale
        held = Document.objects.create(kind='sale', status='held', customer_name='X', subtotal=100, total=107, tax_location='Springfield')
        line(held, 1, '1', '100', '7', cls.item)

    def period(self):
        return ny_periods.resolve('month', '2026-09', date(2026, 9, 30))

    def test_sections_and_totals(self):
        report = sales_tax.build(self.period(), detailed=True)
        q, e = report.sections
        self.assertEqual(q.location, 'Springfield')
        self.assertEqual(q.receipts, 7)   # 6 sales receipts + receipt 4's payment on account
        self.assertEqual(q.gross, Decimal('100') + Decimal('250') + Decimal('1154.90') + Decimal('100') + Decimal('1377.45') - Decimal('100') + Decimal('280.37'))
        self.assertEqual(q.discount, Decimal('577.45'))
        self.assertEqual(q.taxable, Decimal('100') + Decimal('200') + Decimal('577.45') + Decimal('100') + Decimal('1377.45') - Decimal('100') + Decimal('280.37'))
        self.assertEqual(q.nontaxable, Decimal('50'))
        # Receipt 4's $300 payment on account has no linked customer, so no charge history: exactly 7/107 of it = 19.63.
        self.assertEqual(q.tax, Decimal('7') + Decimal('14') + Decimal('40.42') + Decimal('7') + Decimal('96.42') - Decimal('7') + Decimal('19.63'))
        self.assertEqual(q.total, q.gross - q.discount + q.tax)
        self.assertEqual((q.parts['account'].receipts, q.parts['account'].tax, q.parts['account'].total), (1, Decimal('19.63'), Decimal('300')))
        self.assertEqual(e.receipts, 1)
        self.assertEqual((e.gross, e.discount, e.nontaxable, e.taxable, e.tax, e.total),
                         (Decimal('3074.85'), Decimal('153.74'), Decimal('2921.11'), Decimal('0'), Decimal('0'), Decimal('2921.11')))
        self.assertEqual(report.receipts, 8)
        self.assertEqual(report.tax, q.tax)
        self.assertEqual((report.unlinked_deposits, report.unlinked_total), (1, Decimal('500.00')))   # deposit with no order
        self.assertEqual(report.deposits_count, 0)
        self.assertEqual((report.payments_on_account, report.payments_on_account_tax), (Decimal('300.00'), Decimal('19.63')))
        self.assertEqual((report.estimated_count, report.estimated_total, report.estimated_tax), (1, Decimal('300.00'), Decimal('19.63')))
        self.assertEqual((report.charged_to_account, report.charged_to_account_tax), (Decimal('0'), Decimal('0')))
        self.assertEqual(report.other_locations, 0)
        notes = sales_tax_exports._notes(report)
        self.assertTrue(any(n.startswith('Charged to account this period, tax deferred: $0.00') and 'Payments on account this period, tax: $19.63 (on $300.00)' in n
                            and 'Estimated (no charge in the data: history before the backup, or a prepayment held for the customer’s next charge): $19.63' in n for n in notes), notes)

    def test_rows_and_whole_cents(self):
        report = sales_tax.build(self.period(), detailed=True)
        rows = {(r.receipt, r.kind): r for r in report.sections[0].rows}
        row = rows[('3', 'sale')]
        self.assertEqual((row.gross, row.discount, row.taxable, row.nontaxable, row.tax, row.total),
                         (Decimal('1154.90'), Decimal('577.45'), Decimal('577.45'), Decimal('0'), Decimal('40.42'), Decimal('617.87')))
        r4 = rows[('4', 'sale')]
        self.assertEqual((r4.gross, r4.taxable, r4.total), (Decimal('100'), Decimal('100'), Decimal('107')))   # POA stripped
        poa = rows[('4', 'account')]
        self.assertEqual((poa.type_label, poa.gross, poa.taxable, poa.nontaxable, poa.tax, poa.total),
                         ('Payment on account · estimated', Decimal('280.37'), Decimal('280.37'), Decimal('0'), Decimal('19.63'), Decimal('300')))
        self.assertEqual(rows[('5', 'sale')].taxable, Decimal('1377.45'))
        self.assertEqual(rows[('6', 'sale')].type_label, 'Reversal')
        self.assertEqual(rows[('6', 'sale')].taxable, Decimal('-100'))
        self.assertEqual([r.receipt for r in report.sections[0].rows], ['1', '2', '3', '4', '4', '5', '6'])

    def test_summary_has_no_rows_but_same_totals(self):
        detailed = sales_tax.build(self.period(), detailed=True)
        summary = sales_tax.build(self.period(), detailed=False)
        self.assertEqual(summary.tax, detailed.tax)
        self.assertEqual(summary.receipts, detailed.receipts)
        self.assertFalse(summary.detailed)

    def test_quarter_period_includes_september_only_here(self):
        report = sales_tax.build(ny_periods.resolve('quarter', '2027-3', date(2026, 9, 30)))
        self.assertEqual(report.receipts, 9)   # Sep (incl. the payment on account) + Oct 1 receipt

    def test_exports(self):
        report = sales_tax.build(self.period(), detailed=True)
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(sales_tax_exports.xlsx_bytes(report))).active
        cells = [[c.value for c in row] for row in ws.iter_rows()]
        all_row = [r for r in cells if r and r[0] == 'All locations'][0]
        self.assertEqual(Decimal(str(all_row[6])), report.tax.normalize())
        self.assertTrue(any(r and r[1] == '3' and Decimal(str(r[5])) == Decimal('577.45') for r in cells))
        self.assertTrue(any(r and str(r[0]).startswith('Not included above: 1 deposit') for r in cells))
        pdf = sales_tax_exports.pdf_bytes(report)
        self.assertTrue(pdf.startswith(b'%PDF'))
        summary_pdf = sales_tax_exports.pdf_bytes(sales_tax.build(self.period(), detailed=False))
        self.assertTrue(summary_pdf.startswith(b'%PDF'))
        self.assertLess(len(summary_pdf), len(pdf))


class SalesTaxViewTests(AdminModeClientTests):
    @classmethod
    def setUpTestData(cls):
        cls.item = Item.objects.create(sku='X-1', name='Chair', description='Chair', price=Decimal('100'))
        r = receipt(1, (2026, 9, 2), 'Springfield', '100.00', '7.00', '107.00')
        line(r, 1, '1', '100', '7', cls.item)
        r = receipt(2, (2026, 9, 3), 'Exempt', '40.00', '0', '40.00')
        line(r, 1, '1', '40', '0', cls.item)
        cls.brother = Printer.objects.create(name='Test Brother', kind='letter', host='192.0.2.30', port=631)

    def test_options_only(self):
        response = self.client.get('/reports/sales-tax/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sales Tax Quarter')
        self.assertContains(response, 'Sales Tax Year')
        self.assertContains(response, 'your state sales tax quarter or year')
        self.assertContains(response, 'Display Report')
        self.assertIsNone(response.context['report'])

    def test_summary_and_detail(self):
        r = self.client.get('/reports/sales-tax/', {'period': 'month', 'month': '2026-09', 'detail': 'summary', 'run': '1'})
        self.assertContains(r, 'Springfield, IL — Sales Tax 7%')
        self.assertContains(r, 'Exempt — No Sales Tax')
        self.assertContains(r, '$7.00')
        self.assertNotContains(r, 'class="report-section-title"')
        r = self.client.get('/reports/sales-tax/', {'period': 'month', 'month': '2026-09', 'detail': 'detail', 'run': '1'})
        self.assertContains(r, 'class="report-section-title"')
        self.assertContains(r, '<td>1</td>')   # receipt number cell
        self.assertContains(r, 'Direct Print')

    def test_period_kinds(self):
        r = self.client.get('/reports/sales-tax/', {'period': 'quarter', 'quarter': '2027-3', 'run': '1'})
        self.assertEqual((r.context['period'].start, r.context['period'].end), (date(2026, 9, 1), date(2026, 11, 30)))
        r = self.client.get('/reports/sales-tax/', {'period': 'year', 'year': '2027', 'run': '1'})
        self.assertEqual(r.context['period'].start, date(2026, 3, 1))
        r = self.client.get('/reports/sales-tax/', {'period': 'custom', 'start': '2026-09-01', 'end': '2026-09-02', 'run': '1'})
        self.assertEqual(r.context['report'].receipts, 1)
        r = self.client.get('/reports/sales-tax/', {'period': 'bogus', 'run': '1'})
        self.assertEqual(r.status_code, 200)

    def test_today_and_yesterday_periods(self):
        with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 9, 3)):
            r = self.client.get('/reports/sales-tax/', {'period': 'today', 'run': '1'})
            self.assertContains(r, 'Today · 9/3/2026')
            self.assertContains(r, '<option value="today" selected>Today</option>', html=True)
            self.assertEqual(r.context['report'].receipts, 1)   # the 9/3 exempt receipt only
            self.assertEqual(r.context['selected_month'], '2026-09')
            r = self.client.get('/reports/sales-tax/', {'period': 'yesterday', 'run': '1'})
            self.assertContains(r, 'Yesterday · 9/2/2026')
            self.assertContains(r, '<option value="yesterday" selected>Yesterday</option>', html=True)
            self.assertEqual(r.context['report'].receipts, 1)
            self.assertContains(r, '$7.00')
            r = self.client.get('/reports/sales-tax/', {'period': 'yesterday', 'format': 'xlsx'})
            self.assertIn('Sales-Tax-Summary-2026-09-02-2026-09-02.xlsx', r['Content-Disposition'])
            r = self.client.get('/reports/sales-tax/', {'period': 'today', 'detail': 'detail', 'format': 'pdf'})
            self.assertTrue(r.content.startswith(b'%PDF'))
            self.assertIn('Sales-Tax-Detail-2026-09-03-2026-09-03.pdf', r['Content-Disposition'])
        r = self.client.get('/reports/sales-tax/')
        self.assertEqual(r.context['period'].kind, 'month')   # default unchanged

    def test_exports_and_bad_format(self):
        params = {'period': 'month', 'month': '2026-09', 'detail': 'detail', 'run': '1'}
        for fmt, magic in [('xlsx', b'PK'), ('pdf', b'%PDF')]:
            r = self.client.get('/reports/sales-tax/', {**params, 'format': fmt})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.content.startswith(magic))
            self.assertIn('Sales-Tax-Detail-2026-09-01-2026-09-30.' + fmt, r['Content-Disposition'])
        self.assertEqual(self.client.get('/reports/sales-tax/', {**params, 'format': 'csv'}).status_code, 404)

    def test_direct_print(self):
        from sales.test_printing import FakeIPP, ipp_reply
        sent = {}

        def urlopen(request, timeout):
            sent['body'] = request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            r = self.client.post('/reports/sales-tax/print/', {'period': 'month', 'month': '2026-09', 'detail': 'summary', 'printer': self.brother.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn('Test Brother', r.json()['message'])
        self.assertIn(b'RaS2', sent['body'])
        with mock.patch('sales.letter_printing.urllib.request.urlopen') as urlopen:
            self.assertEqual(self.client.post('/reports/sales-tax/print/', {'period': 'month', 'month': '2026-09', 'printer': ''}).status_code, 400)
            r = self.client.post('/reports/sales-tax/print/', {'period': 'month', 'month': '2020-01', 'printer': self.brother.pk})
            self.assertEqual(r.status_code, 400)
        urlopen.assert_not_called()

    def test_index_and_home(self):
        self.assertContains(self.client.get('/reports/'), '/reports/sales-tax/')
        self.assertContains(self.client.get('/'), '/reports/sales-tax/')


class CustomerOrderTaxTests(TestCase):
    """tax on customer orders counts when each payment is collected, in that payment's location."""

    @classmethod
    def setUpTestData(cls):
        from customers.models import Customer
        cls.customer = Customer.objects.create(source_id='c', full_name='PAT EXAMPLE')
        # QB order: 939.90 + 65.79 tax = 1005.69, paid by three deposits and never picked up on a receipt (2020+ practice).
        cls.order = Document.objects.create(kind='order', status='completed', source_id='SO1', source_number=4830, customer_name='PAT EXAMPLE',
                                            subtotal=Decimal('939.90'), tax=Decimal('65.79'), total=Decimal('1005.69'),
                                            deposit_taken=Decimal('1005.69'), deposit_used=Decimal('0'))
        line(cls.order, 1, '2', '469.95', '7')
        for n, day, amount in [(118341, (2026, 8, 30), '250.00'), (119218, (2026, 9, 3), '500.00'), (119219, (2026, 9, 3), '255.69')]:
            d = receipt(n, day, 'Springfield', '0', '0', amount, receipt_type=2, customer='PAT EXAMPLE')
            d.deposit_order = cls.order
            d.save()
        # Order with a non-taxable finish line: 100 taxable + 100 non-taxable, tax 7, total 207; one deposit refund.
        mixed = Document.objects.create(kind='order', status='open', source_id='SO2', source_number=5000, customer_name='MIX',
                                        subtotal=Decimal('200'), tax=Decimal('7'), total=Decimal('207'), deposit_used=Decimal('0'))
        line(mixed, 1, '1', '100', '7')
        line(mixed, 2, '1', '100', '0', name='FINISH')
        for n, amount, kind in [(120001, '207.00', 2), (120002, '-20.70', 3)]:
            d = receipt(n, (2026, 9, 20), 'Springfield', '0', '0', amount, receipt_type=kind, customer='MIX')
            d.deposit_order = mixed
            d.save()
        # 2008-style order picked up on a receipt that used the deposit: the deposit is not counted again.
        old = Document.objects.create(kind='order', status='completed', source_id='SO3', source_number=1, customer_name='OLD',
                                      subtotal=Decimal('100'), tax=Decimal('7'), total=Decimal('107'), deposit_used=Decimal('107'))
        d = receipt(120003, (2026, 9, 21), 'Springfield', '0', '0', '107.00', receipt_type=2, customer='OLD')
        d.deposit_order = old
        d.save()
        # Exempt order deposit.
        exempt = Document.objects.create(kind='order', status='open', source_id='SO4', source_number=5001, customer_name='MUSEUM',
                                         subtotal=Decimal('500'), tax=Decimal('0'), total=Decimal('500'), tax_exempt=True, deposit_used=Decimal('0'))
        d = receipt(120004, (2026, 9, 22), 'Exempt', '0', '0', '250.00', receipt_type=2, customer='MUSEUM')
        d.deposit_order = exempt
        d.save()

    def period(self):
        return ny_periods.resolve('month', '2026-09', date(2026, 9, 30))

    def test_deposits_split_by_order_tax_share_in_their_period(self):
        report = sales_tax.build(self.period(), detailed=True)
        q, e = report.sections
        rows = {r.receipt: r for r in q.rows}
        self.assertNotIn('118341', rows)                    # August deposit belongs to August
        r = rows['119218']
        self.assertEqual((r.kind, r.type_label), ('order', 'Order deposit · Sales Order 4830'))
        self.assertEqual((r.gross, r.taxable, r.nontaxable, r.tax, r.total),
                         (Decimal('467.29'), Decimal('467.29'), Decimal('0.00'), Decimal('32.71'), Decimal('500.00')))   # 500 × 65.79/1005.69 = 32.708…
        self.assertEqual(rows['119219'].tax, Decimal('16.73'))
        mixed = rows['120001']
        self.assertEqual((mixed.tax, mixed.taxable, mixed.nontaxable), (Decimal('7.00'), Decimal('100.00'), Decimal('100.00')))
        refund = rows['120002']
        self.assertEqual((refund.type_label, refund.tax, refund.total), ('Deposit refund · Sales Order 5000', Decimal('-0.70'), Decimal('-20.70')))
        self.assertNotIn('120003', rows)
        self.assertEqual((report.applied_deposits, report.applied_total), (1, Decimal('107.00')))
        orders = q.parts['order']
        self.assertEqual((orders.receipts, orders.tax), (4, Decimal('32.71') + Decimal('16.73') + Decimal('7.00') - Decimal('0.70')))
        self.assertEqual(q.tax, q.parts['sale'].tax + orders.tax)
        self.assertEqual([(x.receipt, x.tax, x.total) for x in e.rows], [('120004', Decimal('0.00'), Decimal('250.00'))])
        self.assertEqual(report.part('order').total, Decimal('500.00') + Decimal('255.69') + Decimal('207.00') - Decimal('20.70') + Decimal('250.00'))

    def test_app_order_payments_count_and_pickups_do_not_double_count(self):
        from sales.models import OrderPayment
        order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='PAT EXAMPLE',
                                        subtotal=Decimal('100'), tax=Decimal('7'), total=Decimal('107'))
        line(order, 1, '1', '100', '7')
        OrderPayment.objects.create(order=order, amount=Decimal('107.00'), method='credit', created_at=datetime(2026, 9, 25, 12, tzinfo=NY))
        pickup = Document.objects.create(kind='sale', status='completed', customer_name='PAT EXAMPLE', source_order=order, payment_method='order',
                                         subtotal=Decimal('100'), tax=Decimal('7'), total=Decimal('107'), completed_at=datetime(2026, 9, 26, 12, tzinfo=NY))
        line(pickup, 1, '1', '100', '7')
        report = sales_tax.build(self.period(), detailed=True)
        q = report.sections[0]
        mine = [r for r in q.rows if r.customer == 'PAT EXAMPLE' and r.date == date(2026, 9, 25)]
        self.assertEqual([(r.type_label, r.tax) for r in mine], [(f'Order payment · {order.numbered_label}', Decimal('7.00'))])
        self.assertEqual(report.native_pickups, 1)
        self.assertEqual(q.parts['sale'].receipts, 0)
        notes = sales_tax_exports._notes(report)
        self.assertTrue(any('picked up on a sales receipt' in n for n in notes))
        self.assertTrue(any('pickup receipt' in n for n in notes))
        for render in (sales_tax_exports.xlsx_bytes, sales_tax_exports.pdf_bytes):
            self.assertTrue(render(report))

    def test_discounted_order_payments_report_the_orders_tax_exactly(self):
        # QB order 5388's figures (4,319.80 - 215.99 + 287.27 = 4,391.08) paid in two deposits; the two payment
        # rows' tax adds up to the order's tax to the cent and the taxable base nets the discount.
        from sales.models import OrderPayment
        from sales.services import save_document
        from sales.models import DiscountReason
        import uuid
        DiscountReason.objects.get_or_create(name='Sale')
        for sku, price in (('6045', '1209.95'), ('6046', '529.95'), ('6047', '1369.95')):
            Item.objects.create(sku=sku, name=f'ITEM {sku}', description=f'ITEM {sku}', price=Decimal(price), on_hand=5, tax_rate=Decimal('7'))
        items = {i.sku: i for i in Item.objects.filter(sku__in=['6045', '6046', '6047'])}
        order = save_document(dict(action='save', version=0, customer_id=self.customer.pk, discount_percent='5', discount_reason='Sale',
                                   lines=[dict(item_id=items['6045'].pk, quantity='2', price='1209.95'), dict(item_id=items['6046'].pk, quantity='1', price='529.95'),
                                          dict(item_id=items['6047'].pk, quantity='1', price='1369.95')]), 'order', uuid.uuid4())
        self.assertEqual((order.discount, order.tax, order.total), (Decimal('215.99'), Decimal('287.27'), Decimal('4391.08')))
        OrderPayment.objects.create(order=order, amount=Decimal('1097.77'), method='credit', created_at=datetime(2026, 9, 25, 12, tzinfo=NY))
        OrderPayment.objects.create(order=order, amount=Decimal('3293.31'), method='check', created_at=datetime(2026, 9, 27, 12, tzinfo=NY))
        report = sales_tax.build(self.period(), detailed=True)
        q = report.sections[0]
        mine = [r for r in q.rows if r.type_label == f'Order payment · {order.numbered_label}']
        self.assertEqual(len(mine), 2)
        self.assertEqual(sum((r.tax for r in mine), Decimal('0')), Decimal('287.27'))
        self.assertEqual(sum((r.total for r in mine), Decimal('0')), Decimal('4391.08'))
        self.assertEqual(sum((r.taxable for r in mine), Decimal('0')), Decimal('4103.81'))

    def test_dollar_discounted_order_payments_report_the_orders_tax_exactly_and_tie_to_daily_totals(self):
        # ($): the same order with $50 off instead of 5 % (4,319.80 - 50.00 + 298.89 = 4,568.69), paid in two
        # deposits; Sales Tax's two payment rows and Daily Totals' tax for the month both equal the order's tax.
        from sales.models import DiscountReason, OrderPayment
        from sales.services import save_document
        from . import daily_totals
        import uuid
        DiscountReason.objects.get_or_create(name='Sale')
        for sku, price in (('6045', '1209.95'), ('6046', '529.95'), ('6047', '1369.95')):
            Item.objects.create(sku=sku, name=f'ITEM {sku}', description=f'ITEM {sku}', price=Decimal(price), on_hand=5, tax_rate=Decimal('7'))
        items = {i.sku: i for i in Item.objects.filter(sku__in=['6045', '6046', '6047'])}
        order = save_document(dict(action='save', version=0, customer_id=self.customer.pk, discount_percent='0', discount_amount='50', discount_reason='Sale',
                                   lines=[dict(item_id=items['6045'].pk, quantity='2', price='1209.95'), dict(item_id=items['6046'].pk, quantity='1', price='529.95'),
                                          dict(item_id=items['6047'].pk, quantity='1', price='1369.95')]), 'order', uuid.uuid4())
        self.assertEqual((order.discount, order.discount_percent, order.tax, order.total), (Decimal('50.00'), Decimal('0'), Decimal('298.89'), Decimal('4568.69')))
        OrderPayment.objects.create(order=order, amount=Decimal('1142.17'), method='credit', created_at=datetime(2026, 9, 25, 12, tzinfo=NY))
        OrderPayment.objects.create(order=order, amount=Decimal('3426.52'), method='check', created_at=datetime(2026, 9, 27, 12, tzinfo=NY))
        before = sales_tax.build(self.period(), detailed=True).sections[0]
        mine = [r for r in before.rows if r.type_label == f'Order payment · {order.numbered_label}']
        self.assertEqual(len(mine), 2)
        self.assertEqual((sum((r.tax for r in mine), Decimal('0')), sum((r.total for r in mine), Decimal('0')), sum((r.taxable for r in mine), Decimal('0'))),
                         (Decimal('298.89'), Decimal('4568.69'), Decimal('4269.80')))
        daily = daily_totals.build(self.period())
        self.assertEqual(daily.totals['tax'], sum((s.tax for s in sales_tax.build(self.period(), detailed=True).sections), Decimal('0')))
