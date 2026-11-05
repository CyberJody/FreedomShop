# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scanner Test page: QR size sheet, direct print to the office Brother Tray 1, scan log."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from sales.models import Printer
from accounts.tests import MIDDLEWARE, make_admin, make_register
from . import scanner_test


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class ScannerTestPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin('TEST Admin', password='TEST-admin')
        cls.register = make_register()
        cls.office = Printer.objects.create(name='TEST Office Brother', kind='letter', host='127.0.0.1', color=True, media_source='tray-1')
        cls.tray2 = Printer.objects.create(name='TEST Office Brother – Tray 2', kind='letter', host='127.0.0.1', media_source='tray-2')
        cls.counter = Printer.objects.create(name='TEST Counter Brother', kind='letter', host='127.0.0.2')

    def test_admin_only(self):
        self.client.force_login(self.admin)
        page = self.client.get('/scanner-test/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="scan-box"')
        self.assertContains(page, 'FS-TEST-0001')
        self.assertContains(page, 'same as a PO item label (SL548)')
        self.assertContains(page, 'TEST Office Brother (Tray 1)')
        self.assertNotContains(page, 'TEST Office Brother – Tray 2</option>')  # listed, but not the default
        self.assertEqual(page.context['printer'], self.office)
        self.assertContains(self.client.get('/'), '/scanner-test/')
        self.assertEqual(self.client.get('/scanner-test/sheet.pdf')['Content-Type'], 'application/pdf')
        self.client.logout()
        self.client.force_login(self.register)
        self.assertEqual(self.client.get('/scanner-test/').status_code, 302)  # Admin Mode redirect, like Server Status
        self.assertEqual(self.client.get('/scanner-test/sheet.pdf').status_code, 302)
        self.assertEqual(self.client.post('/scanner-test/print/', {'printer': self.office.pk}).status_code, 403)
        self.assertNotContains(self.client.get('/'), '/scanner-test/')

    def test_sheet_pdf_has_every_size(self):
        pdf = scanner_test.sheet_pdf()
        self.assertTrue(pdf.startswith(b'%PDF'))
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf)
        self.assertEqual(len(doc), 1)
        text = doc[0].get_textpage().get_text_range()
        for payload, _, _ in scanner_test.SHEET:
            self.assertIn(payload.replace('https://', ''), text)  # small captions drop the scheme to fit the column
        doc.close()

    def test_print_goes_to_office_tray_1_by_default(self):
        self.client.force_login(self.admin)
        with mock.patch('reports.direct_print.print_report', return_value='Sent 1 page to TEST Office Brother at 1:00 PM.') as sent:
            response = self.client.post('/scanner-test/print/', {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent.call_args[0][0], self.office)
        self.assertTrue(sent.call_args[0][1].startswith(b'%PDF'))
        self.assertEqual(sent.call_args[0][2], 'Scanner Test sheet')
        self.assertIn('Sent 1 page', response.json()['message'])
        with mock.patch('reports.direct_print.print_report', return_value='ok') as sent:
            self.client.post('/scanner-test/print/', {'printer': self.counter.pk})
        self.assertEqual(sent.call_args[0][0], self.counter)
        with mock.patch('reports.direct_print.print_report', return_value='ok') as sent:
            self.client.post('/scanner-test/print/', {'printer': '999999'})
        self.assertEqual(sent.call_args[0][0], self.office)  # unknown key → default
        self.assertEqual(self.client.get('/scanner-test/print/').status_code, 405)

    def test_default_printer_rules(self):
        office_tray1_row = Printer(name='O1', kind='letter', host='h', media_source='tray-1')
        office_color = Printer(name='O', kind='letter', host='h', color=True)
        other_tray1 = Printer(name='X', kind='letter', host='x', media_source='tray-1')
        first = Printer(name='A', kind='letter', host='a')
        self.assertEqual(scanner_test.default_printer([first, office_color, office_tray1_row]), office_tray1_row)
        self.assertEqual(scanner_test.default_printer([first, office_color]), office_color)
        self.assertEqual(scanner_test.default_printer([first, other_tray1]), other_tray1)
        self.assertEqual(scanner_test.default_printer([first]), first)
        self.assertIsNone(scanner_test.default_printer([]))

    def test_svg_and_matrices(self):
        for payload, _, _ in scanner_test.SHEET:
            m = scanner_test.matrices()[payload]
            self.assertEqual(len(m['rows']), m['size'])
            self.assertTrue(all(len(r) == m['size'] for r in m['rows']))
            self.assertGreaterEqual(m['size'], 29)  # standard QR with quiet zone, never Micro QR
        self.assertIn('<svg', scanner_test.svg('FS-TEST-0002', 72))
