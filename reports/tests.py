# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sales by Vendor report: grouping, exclusions, exports. Runs on the isolated test database."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
from decimal import Decimal
from io import BytesIO

from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin

from purchasing.models import Vendor
from sales.models import Document, Item, Line

from . import exports, sales_by_vendor


def sale(number, when, customer='CUSTOMER', **extra):
    extra.setdefault('receipt_type', 0)
    doc = Document.objects.create(kind='sale', status='completed', source_id=f'R{number}', source_number=number,
                                  customer_name=customer, completed_at=datetime(*when, 15, 0, tzinfo=NY),
                                  subtotal=0, tax=0, total=0, **extra)
    return doc


def line(doc, position, item, qty, price, original=None, reason='', **extra):
    return Line.objects.create(document=doc, position=position, item=item, sku=item.sku if item else extra.pop('sku', ''),
                               item_name=item.name if item else extra.pop('item_name', ''), description=extra.pop('description', 'desc'),
                               quantity=Decimal(qty), price=Decimal(price), tax_rate=7, source_id=f'{doc.pk}-{position}',
                               original_price=Decimal(original) if original is not None else None, discount_reason=reason)


class SalesByVendorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.byler = Vendor.objects.create(company='DEMO WOODSHOP', code='BW')
        cls.capitol = Vendor.objects.create(company='SAMPLE IMPORTING CO', code='CAP')
        cls.chair = Item.objects.create(sku='BW-CHAIR', name='Oak chair', description='Oak chair', price=Decimal('199.95'), primary_vendor=cls.byler)
        cls.table = Item.objects.create(sku='BW-TABLE', name='Oak table', description='Oak table', price=Decimal('899.95'), primary_vendor=cls.byler)
        cls.rug = Item.objects.create(sku='CAP-RUG', name='Rug', description='Rug', price=Decimal('59.95'), primary_vendor=cls.capitol)
        cls.orphan = Item.objects.create(sku='NOVEND', name='No vendor item', description='x', price=Decimal('10'))

        r1 = sale(1001, (2026, 9, 3), 'SMITH')
        line(r1, 1, cls.chair, '2', '199.95', original='199.95')
        line(r1, 2, cls.rug, '1', '59.95', original='49.95')   # sold above regular price: not a discount
        r2 = sale(1002, (2026, 9, 20), 'JONES')
        line(r2, 1, cls.table, '1', '854.952500', original='899.95', reason='Sale')   # discounted, 6-place source price
        line(r2, 2, cls.orphan, '1', '10')
        line(r2, 3, None, '1', '250', sku='', item_name='Payment On Account', description='Payment On Account')
        line(r2, 4, None, '1', '99', sku='GONE', item_name='Deleted in QB', description='deleted item')
        r3 = sale(1003, (2026, 9, 25), 'SMITH', receipt_type=1)   # return
        line(r3, 1, cls.chair, '-1', '199.95')
        # Outside the range, a held receipt, and a sales order: all excluded.
        line(sale(900, (2026, 8, 31)), 1, cls.chair, '5', '199.95')
        line(sale(1100, (2026, 10, 1)), 1, cls.chair, '5', '199.95')
        held = Document.objects.create(kind='sale', status='held', customer_name='X', subtotal=0, total=0)
        line(held, 1, cls.chair, '5', '199.95')
        order = Document.objects.create(kind='order', status='open', customer_name='X', subtotal=0, total=0,
                                        completed_at=datetime(2026, 9, 10, tzinfo=NY))
        line(order, 1, cls.chair, '5', '199.95')
        # Deposit receipt has no item lines.
        sale(1004, (2026, 9, 12), receipt_type=2)

    def test_groups_by_vendor_with_net_totals(self):
        report = sales_by_vendor.build(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual([g.vendor for g in report.groups], ['DEMO WOODSHOP', 'SAMPLE IMPORTING CO', '(No vendor)'])
        byler = report.groups[0]
        self.assertEqual([(r.receipt, r.sku, r.quantity) for r in byler.rows],
                         [('1001', 'BW-CHAIR', Decimal('2')), ('1002', 'BW-TABLE', Decimal('1')), ('1003', 'BW-CHAIR', Decimal('-1'))])
        self.assertEqual(byler.quantity, Decimal('2'))
        self.assertEqual(byler.amount, Decimal('399.90') + Decimal('854.95') - Decimal('199.95'))
        self.assertEqual(report.groups[1].amount, Decimal('59.95'))
        self.assertEqual(report.groups[2].amount, Decimal('10.00'))
        self.assertEqual(report.amount, sum(g.amount for g in report.groups))
        self.assertEqual(report.receipts, 3)
        self.assertEqual(report.lines, 5)

    def test_excludes_unlinked_lines_orders_held_and_out_of_range(self):
        report = sales_by_vendor.build(date(2026, 9, 1), date(2026, 9, 30))
        skus = {r.sku for g in report.groups for r in g.rows}
        self.assertNotIn('GONE', skus)
        self.assertNotIn('', skus)
        self.assertEqual(sum(len(g.rows) for g in report.groups), 5)

    def test_single_vendor_filter(self):
        report = sales_by_vendor.build(date(2026, 9, 1), date(2026, 9, 30), vendor=self.capitol)
        self.assertEqual([g.vendor for g in report.groups], ['SAMPLE IMPORTING CO'])
        self.assertEqual(report.amount, Decimal('59.95'))
        self.assertIn('SAMPLE IMPORTING CO', report.subtitle)

    def test_prices_show_cents_but_extend_exactly(self):
        doc = sale(3000, (2026, 9, 23))
        line(doc, 1, self.chair, '1', '1377.4525', original='1449.95', reason='Custom')
        line(doc, 2, self.chair, '2', '569.9525', original='599.95', reason='Custom')
        line(doc, 3, self.chair, '3', '0.999', original='1')   #.999 shows as 1.00; 3 x 0.999 = 2.997 -> 3.00
        report = sales_by_vendor.build(date(2026, 9, 23), date(2026, 9, 23))
        rows = report.groups[0].rows
        # Price column to the cent; amount = qty x exact price, half-up (QB: 2 x 569.9525 = 1139.905 -> 1139.91),.
        self.assertEqual([(r.price, r.amount) for r in rows],
                         [(Decimal('1377.45'), Decimal('1377.45')), (Decimal('569.95'), Decimal('1139.91')), (Decimal('1.00'), Decimal('3.00'))])
        self.assertEqual(report.amount, Decimal('2520.36'))
        self.assertEqual([r.discounted for r in rows], [True, True, True])  # 0.999 is below the 1.00 regular price

    def test_date_bounds_are_inclusive_local_days(self):
        report = sales_by_vendor.build(date(2026, 9, 3), date(2026, 9, 3))
        self.assertEqual(report.receipts, 1)
        self.assertEqual(report.lines, 2)

    def test_empty_range(self):
        report = sales_by_vendor.build(date(2020, 1, 1), date(2020, 1, 2))
        self.assertEqual(report.groups, [])
        self.assertEqual(report.amount, Decimal('0'))

    def test_discount_flag(self):
        report = sales_by_vendor.build(date(2026, 9, 1), date(2026, 9, 30))
        flags = {(r.receipt, r.sku): r.discounted for g in report.groups for r in g.rows}
        self.assertTrue(flags[('1002', 'BW-TABLE')])
        self.assertFalse(flags[('1001', 'BW-CHAIR')])
        self.assertFalse(flags[('1001', 'CAP-RUG')])
        self.assertFalse(flags[('1002', 'NOVEND')])   # no original price recorded
        self.assertEqual(report.discounted_lines, 1)

    def test_exports_agree_with_screen(self):
        report = sales_by_vendor.build(date(2026, 9, 1), date(2026, 9, 30))
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(exports.xlsx_bytes(report)))
        ws = wb.active
        cells = [[c.value for c in row] for row in ws.iter_rows()]
        totals = [row for row in cells if row and row[0] == 'Total DEMO WOODSHOP']
        self.assertEqual(Decimal(str(totals[0][8])), Decimal('1054.9'))
        grand = [row for row in cells if row and row[0] == 'Grand total'][0]
        self.assertEqual(Decimal(str(grand[8])), report.amount.normalize())
        self.assertEqual(ws.print_title_rows, '$6:$6')
        note = [row[0] for row in ws.iter_rows() if str(row[0].value) == exports.DISCOUNT_NOTE][0]
        self.assertEqual(str(note.value), exports.DISCOUNT_NOTE)
        discounted = [row for row in ws.iter_rows() if row[3].value == 'BW-TABLE'][0]
        self.assertEqual(discounted[3].font.color.rgb[-6:], exports.RED)
        self.assertNotIn('*', discounted[7].number_format)

        pdf = exports.pdf_bytes(report)
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertGreater(len(pdf), 1500)

    def test_exports_without_discounts_have_no_note(self):
        report = sales_by_vendor.build(date(2026, 9, 3), date(2026, 9, 3))
        self.assertEqual(report.discounted_lines, 0)
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(exports.xlsx_bytes(report))).active
        self.assertNotIn(exports.DISCOUNT_NOTE, [str(row[0].value) for row in ws.iter_rows()])

    def test_pdf_handles_long_and_special_descriptions(self):
        doc = sale(2000, (2026, 9, 15))
        line(doc, 1, self.chair, '1', '1', description='Oak & <maple> chair\nsecond line ' + 'x' * 400)
        report = sales_by_vendor.build(date(2026, 9, 15), date(2026, 9, 15))
        self.assertTrue(exports.pdf_bytes(report).startswith(b'%PDF'))


class SalesByVendorSummaryTests(TestCase):
    """QB-style Vendor Sales Summary (one line per vendor) and per-item drill-down, with cost and margin."""
    setUpTestData = classmethod(SalesByVendorTests.setUpTestData.__func__)

    def test_item_summary_nets_returns_and_figures_margin(self):
        Line.objects.filter(item=self.chair).update(unit_cost=Decimal('100.009'))
        Line.objects.filter(item=self.table).update(unit_cost=Decimal('500'))
        report = sales_by_vendor.summarize(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual([g.label for g in report.groups], ['DEMO WOODSHOP', 'SAMPLE IMPORTING CO', '(No vendor)'])
        byler = report.groups[0]
        self.assertEqual([(r['name'], r['quantity']) for r in byler.rows], [('Oak chair', Decimal('1')), ('Oak table', Decimal('1'))])
        chair = byler.rows[0]
        self.assertEqual((chair['amount'], chair['cost']), (Decimal('199.95'), Decimal('100.01')))   # 2 sold, 1 returned
        self.assertEqual(chair['margin_pct'], Decimal('49.98'))                                     # 99.95/199.95, truncated
        self.assertEqual(byler.totals['amount'], Decimal('1054.90'))
        self.assertEqual(report.totals['amount'], Decimal('1124.85'))
        self.assertEqual((report.receipts, report.lines), (3, 5))
        self.assertIsNone(sales_by_vendor.margin_pct(Decimal('0'), Decimal('0')))

    def test_vendor_totals_one_line_each(self):
        report = sales_by_vendor.summarize(date(2026, 9, 1), date(2026, 9, 30), view='vendors')
        rows = report.groups[0].rows
        self.assertEqual([(r['vendor'], r['quantity'], r['amount']) for r in rows],
                         [('DEMO WOODSHOP', Decimal('2'), Decimal('1054.90')), ('SAMPLE IMPORTING CO', Decimal('1'), Decimal('59.95')),
                          ('(No vendor)', Decimal('1'), Decimal('10.00'))])
        self.assertEqual(rows[0]['margin_pct'], Decimal('100.00'))   # no cost recorded, like QB
        single = sales_by_vendor.summarize(date(2026, 9, 1), date(2026, 9, 30), vendor=self.capitol)
        self.assertEqual([g.label for g in single.groups], ['SAMPLE IMPORTING CO'])


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class AdminModeClientTests(TestCase):
    """Report screens are for signed-in admins only; these tests sign a TEST admin in on the test client."""

    def setUp(self):
        sign_in_admin(self.client)


class SalesByVendorViewTests(AdminModeClientTests):
    @classmethod
    def setUpTestData(cls):
        cls.vendor = Vendor.objects.create(company='DEMO WOODSHOP', code='BW')
        cls.item = Item.objects.create(sku='BW-1', name='Chair', description='Chair', price=Decimal('5'), primary_vendor=cls.vendor)
        line(sale(1, (2026, 9, 5)), 1, cls.item, '3', '5')
        line(sale(2, (2026, 9, 6)), 1, cls.item, '1', '1234.5', original='1500')

    def test_form_without_run_shows_options_only(self):
        response = self.client.get('/reports/sales-by-vendor/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Display Report')
        self.assertNotContains(response, 'Grand total')
        self.assertIsNone(response.context['report'])

    def test_display_and_exports(self):
        params = {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1', 'view': 'detail'}
        response = self.client.get('/reports/sales-by-vendor/', params)
        self.assertContains(response, 'DEMO WOODSHOP')
        self.assertContains(response, '$15.00')
        self.assertContains(response, '$1,234.50')
        self.assertContains(response, '$1,249.50')
        self.assertContains(response, 'class="discounted"', count=1)
        self.assertContains(response, '$1,234.50</td>')
        self.assertNotContains(response, '$1,234.50<span')
        self.assertContains(response, '<span class="mark">*</span> Sold at Discounted Price')
        self.assertContains(self.client.get('/reports/sales-by-vendor/', {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1', 'view': 'detail'}), 'Grand total')
        self.assertContains(response, 'format=xlsx')
        self.assertNotContains(response, 'format=csv')
        for fmt, ctype, magic in [('xlsx', 'application/vnd.openxmlformats', b'PK'), ('pdf', 'application/pdf', b'%PDF')]:
            r = self.client.get('/reports/sales-by-vendor/', {**params, 'format': fmt})
            self.assertEqual(r.status_code, 200, fmt)
            self.assertTrue(r['Content-Type'].startswith(ctype), fmt)
            self.assertTrue(r.content.startswith(magic), fmt)
            self.assertIn('attachment; filename="Sales-by-Vendor-All-2026-09-01-2026-09-30.' + fmt, r['Content-Disposition'])

    def test_summary_views_on_screen_and_exports(self):
        # default view totals each item instead of listing every receipt.
        params = {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1'}
        r = self.client.get('/reports/sales-by-vendor/', params)
        self.assertEqual(r.context['view'], 'items')
        self.assertContains(r, 'Qty Sold')
        self.assertContains(r, '<td class="number">4</td>')          # 3 + 1 chairs on one line
        self.assertNotContains(r, 'Receipt #')
        self.assertContains(r, 'Total DEMO WOODSHOP')
        r = self.client.get('/reports/sales-by-vendor/', {**params, 'view': 'vendors'})
        self.assertContains(r, f'vendor={self.vendor.pk}')                # drill-down link to the vendor's items
        self.assertContains(r, '$1,249.50')
        for view in ('items', 'vendors'):
            for fmt, magic in [('xlsx', b'PK'), ('pdf', b'%PDF')]:
                resp = self.client.get('/reports/sales-by-vendor/', {**params, 'view': view, 'format': fmt})
                self.assertTrue(resp.content.startswith(magic), (view, fmt))

    def test_vendor_filter_and_bad_inputs(self):
        r = self.client.get('/reports/sales-by-vendor/', {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1', 'vendor': self.vendor.pk})
        self.assertContains(r, 'Total DEMO WOODSHOP')
        self.assertEqual(self.client.get('/reports/sales-by-vendor/', {'vendor': '999999', 'run': 1}).status_code, 404)
        self.assertEqual(self.client.get('/reports/sales-by-vendor/', {'vendor': 'abc'}).status_code, 404)
        self.assertEqual(self.client.get('/reports/sales-by-vendor/', {'format': 'docx', 'run': 1}).status_code, 404)
        self.assertEqual(self.client.get('/reports/sales-by-vendor/', {'format': 'csv', 'run': 1}).status_code, 404)
        # Reversed dates are swapped, garbage dates fall back to the current month.
        r = self.client.get('/reports/sales-by-vendor/', {'start': '2026-09-30', 'end': '2026-09-01', 'run': '1'})
        self.assertEqual((r.context['start'], r.context['end']), (date(2026, 9, 1), date(2026, 9, 30)))
        r = self.client.get('/reports/sales-by-vendor/', {'start': 'nope', 'end': '2026-09-30'})
        self.assertEqual(r.status_code, 200)

    def test_index_and_home_link(self):
        self.assertContains(self.client.get('/reports/'), 'Sales by Vendor')
        self.assertContains(self.client.get('/'), '/reports/sales-by-vendor/')


class DirectPrintTests(AdminModeClientTests):
    """Direct Print rasterizes the report PDF and sends it through the Letter (IPP / PWG raster) path."""

    @classmethod
    def setUpTestData(cls):
        from sales.models import Printer
        cls.vendor = Vendor.objects.create(company='DEMO WOODSHOP', code='BW')
        cls.item = Item.objects.create(sku='BW-1', name='Chair', description='Chair', price=Decimal('5'), primary_vendor=cls.vendor)
        line(sale(1, (2026, 9, 5)), 1, cls.item, '3', '5', original='5')
        cls.brother = Printer.objects.create(name='Test Brother', kind='letter', host='192.0.2.30', port=631)
        cls.other = Printer.objects.create(name='Other Brother', kind='letter', host='192.0.2.31', port=631)
        cls.star = Printer.objects.create(name='Test Star', kind='receipt', host='192.0.2.40', port=9100)
        Printer.objects.create(name='Retired Brother', kind='letter', host='192.0.2.32', port=631, active=False)

    def ipp_ok(self):
        from unittest import mock
        from sales.test_printing import FakeIPP, ipp_reply
        sent = {}

        def urlopen(request, timeout):
            sent['url'], sent['body'] = request.full_url, request.data
            return FakeIPP(ipp_reply())
        return mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen), sent

    def test_pdf_pages_become_portrait_letter_sheets(self):
        from sales.letter_printing import PAGE_H, PAGE_W
        from . import direct_print
        report = sales_by_vendor.build(date(2026, 9, 1), date(2026, 9, 30))
        pages = direct_print.pages_from_pdf(exports.pdf_bytes(report))
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].size, (PAGE_W, PAGE_H))
        self.assertEqual(pages[0].mode, 'L')
        self.assertLess(min(pages[0].getdata()), 128)   # something was actually drawn

    def test_export_bar_lists_letter_printers_only(self):
        response = self.client.get('/reports/sales-by-vendor/', {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1'})
        self.assertContains(response, 'Direct Print')
        self.assertContains(response, 'Test Brother')
        self.assertContains(response, 'Other Brother')
        self.assertNotContains(response, 'Test Star')
        self.assertNotContains(response, 'Retired Brother')

    def test_direct_print_sends_ipp_job_and_remembers_printer(self):
        patcher, sent = self.ipp_ok()
        with patcher:
            response = self.client.post('/reports/sales-by-vendor/print/', {'start': '2026-09-01', 'end': '2026-09-30', 'vendor': '', 'printer': self.other.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('Sent 1 page to Other Brother', response.json()['message'])
        self.assertEqual(sent['url'], 'http://192.0.2.31:631/ipp/print')
        self.assertIn(b'image/pwg-raster', sent['body'])
        self.assertIn(b'RaS2', sent['body'])
        self.assertIn(b'Sales by Vendor', sent['body'])   # job-name
        response = self.client.get('/reports/sales-by-vendor/', {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1'})
        self.assertContains(response, f'<option value="{self.other.pk}" selected>')

    def test_direct_print_validation_and_failures(self):
        from unittest import mock
        from sales.test_printing import FakeIPP, ipp_reply
        params = {'start': '2026-09-01', 'end': '2026-09-30', 'vendor': ''}
        with mock.patch('sales.letter_printing.urllib.request.urlopen') as urlopen:
            self.assertEqual(self.client.post('/reports/sales-by-vendor/print/', {**params, 'printer': ''}).status_code, 400)
            self.assertEqual(self.client.post('/reports/sales-by-vendor/print/', {**params, 'printer': self.star.pk}).status_code, 400)
            self.assertEqual(self.client.post('/reports/sales-by-vendor/print/', {**params, 'printer': '999999'}).status_code, 400)
            self.assertEqual(self.client.post('/reports/sales-by-vendor/print/', {**params, 'vendor': '999999', 'printer': self.brother.pk}).status_code, 404)
            r = self.client.post('/reports/sales-by-vendor/print/', {'start': '2020-01-01', 'end': '2020-01-02', 'vendor': '', 'printer': self.brother.pk})
            self.assertEqual(r.status_code, 400)
            self.assertIn('Nothing to print', r.json()['error'])
            self.assertEqual(self.client.get('/reports/sales-by-vendor/print/').status_code, 405)
        urlopen.assert_not_called()
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=OSError):
            r = self.client.post('/reports/sales-by-vendor/print/', {**params, 'printer': self.brother.pk})
        self.assertEqual(r.status_code, 502)
        self.assertIn('Could not reach Test Brother', r.json()['error'])
        with mock.patch('sales.letter_printing.urllib.request.urlopen', return_value=FakeIPP(ipp_reply(0x0400))):
            r = self.client.post('/reports/sales-by-vendor/print/', {**params, 'printer': self.brother.pk})
        self.assertEqual(r.status_code, 502)
        self.assertIn('refused the job', r.json()['error'])

    def test_no_letter_printers_disables_direct_print(self):
        from sales.models import Printer
        Printer.objects.filter(kind='letter').update(active=False)
        response = self.client.get('/reports/sales-by-vendor/', {'start': '2026-09-01', 'end': '2026-09-30', 'run': '1'})
        self.assertContains(response, 'No Letter printers')
        self.assertContains(response, 'id="direct-print" disabled')
