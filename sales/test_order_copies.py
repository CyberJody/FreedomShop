# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each Letter printer has its own tray and sales order copy count, configured by the shop."""
import importlib
import uuid
from unittest import mock

from django.test import TestCase

from customers.models import Customer
from . import letter_printing
from .models import Item, Printer
from .services import save_document

MIGRATION = importlib.import_module('sales.migrations.0044_printer_tray3_order_copies')
TRAY3 = 'Brother MFC Office \u2013 Tray 3'


class OfficeTray3Migration(TestCase):
    def test_fork_migration_has_no_printer_data_step(self):
        from django.db import migrations
        self.assertFalse(any(isinstance(op, migrations.RunPython) for op in MIGRATION.Migration.operations))

    def test_tray_choices_and_form_field(self):
        self.assertIn(('tray-3', 'Tray 3'), Printer.MEDIA_SOURCES)
        self.assertIn(('tray-4', 'Tray 4'), Printer.MEDIA_SOURCES)
        page = self.client.get('/printers/new/').content.decode()
        self.assertIn('Sales order copies', page)
        self.assertIn('value="tray-3"', page)
        data = dict(name='TEST Tray 4', kind='letter', host='192.0.2.40', port='631', media_source='tray-4', order_copies='1', active='on')
        for bad in ('0', '6'):
            response = self.client.post('/printers/new/', {**data, 'order_copies': bad})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(Printer.objects.filter(name='TEST Tray 4').exists())
        self.client.post('/printers/new/', data)
        printer = Printer.objects.get(name='TEST Tray 4')
        self.assertEqual((printer.media_source, printer.order_copies), ('tray-4', 1))
        # a blank box (or an old form without it) keeps the printer's number
        self.client.post(f'/printers/{printer.pk}/', {**data, 'order_copies': '', 'version': printer.version})
        printer.refresh_from_db()
        self.assertEqual(printer.order_copies, 1)


class OrderCopiesPerPrinter(TestCase):
    def setUp(self):
        Printer.objects.filter(kind='letter').delete()
        self.east = Printer.objects.create(name='East Counter Brother', kind='letter', host='192.0.2.21', port=631)
        self.tray3 = Printer.objects.create(name=TRAY3, kind='letter', host='192.0.2.22', port=631, color=True,
                                            media_source='tray-3', order_copies=1)
        customer = Customer.objects.create(source_id='r85', first_name='Pat', last_name='Buyer', full_name='Pat Buyer')
        item = Item.objects.create(sku='R85-1', name='TEST R85 CHAIR', description='TEST R85 CHAIR', price='100.00', tax_rate='7')
        self.order = save_document(dict(action='save', customer_id=customer.pk, lines=[dict(item_id=item.pk, quantity='1', price='100.00', tax_rate='7')]), 'order', uuid.uuid4())

    def test_order_page_offers_each_printers_copies(self):
        page = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
        self.assertIn(f'<option value="{self.tray3.pk}" data-copies="1">{TRAY3}</option>', page)
        self.assertIn(f'<option value="{self.east.pk}" data-copies="3">East Counter Brother</option>', page)
        # the first printer in the list (Brother MFC Office - Tray 3 sorts before East) sets the starting number;
        # the script then follows the remembered or picked printer
        self.assertIn('<select id="print-copies" autocomplete="off"><option selected>1</option><option>2</option>', page)
        self.assertIn('const printerCopies=()=>', page)
        self.assertIn("receiptPrinter?.addEventListener('change',()=>{printerCopies();rememberChoice();});", page)
        receipt_page = self.client.get('/pos/sale/new/').content.decode()
        self.assertNotIn('data-copies=', receipt_page)

    def test_tray3_job_asks_for_tray_3_and_one_copy(self):
        sent = {}

        def fake_ipp(printer, operation, *parts, **kwargs):
            if operation == 0x000B:
                return 0, {'media-source-supported': ['auto', 'by-pass-tray', 'tray-1', 'tray-2', 'tray-3', 'tray-4']}
            sent['job'] = parts
            return 0, {}

        with mock.patch('sales.letter_printing.ipp_request', side_effect=fake_ipp), \
             mock.patch('sales.letter_printing.copy_sets', wraps=letter_printing.copy_sets) as sets:
            response = self.client.post(f'/pos/order/{self.order.token}/print/', {'printer': self.tray3.pk, 'copies': '1'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(f'to {TRAY3} (1 copy)', response.json()['message'])
        self.assertIn(b'tray-3', sent['job'][1])
        self.assertEqual(sets.call_args.args[1], 1)
        self.assertEqual(self.client.post(f'/pos/order/{self.order.token}/print/', {'printer': self.tray3.pk, 'copies': '6'}).status_code, 400)
