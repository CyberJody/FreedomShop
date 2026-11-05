# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from customers.models import Customer
from .models import Item, Printer
from .printing import WIDTH, PrintError, printer_status, render_receipt, render_test_page, star_raster
from .services import save_document
from .letter_printing import STORE_BLOCK

STORE_BLOCK_LAST = STORE_BLOCK[-1]
READY = bytes.fromhex('2386000000000000000000')


class FakeConnection:
    def __init__(self, reply=READY):
        self.sent = b''
        self.reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def sendall(self, data):
        self.sent += data

    def settimeout(self, value):
        pass

    def recv(self, size):
        return self.reply


class ReceiptPrinting(TestCase):
    def setUp(self):
        self.printer = Printer.objects.create(name='TEST Receipt Printer', kind='receipt', host='192.0.2.10', port=9100)
        self.customer = Customer.objects.create(source_id='print-test', first_name='Pat', last_name='Buyer', full_name='Pat Buyer', street='1 Main St', city='Springfield', state='NY', postal_code='62701')
        self.item = Item.objects.create(sku='SVP-FS13-D', name='Sunflower', description='13" POLY SUNFLOWER W/STEM, ASSORTED COLORS: 1 YELLOW, 1 ORANGE, and a long description that must wrap', price=54.95, on_hand=5, tax_rate=7)

    def sale(self, action='complete'):
        data = dict(action=action, customer_id=self.customer.pk, lines=[dict(item_id=self.item.pk, quantity='2', price='54.95', tax_rate='7')])
        if action == 'complete':
            data['payments'] = [dict(method='cash', amount='150.00')]
        return save_document(data, 'sale', uuid.uuid4())

    def test_receipt_renders_full_width_with_testing_marks(self):
        img = render_receipt(self.sale(), reprint=True)
        self.assertEqual(img.width, WIDTH)
        self.assertGreater(img.height, 900)
        # Testing banners are solid black bars across the full width at the top and bottom.
        self.assertEqual(img.getpixel((0, 10)), 0)
        self.assertEqual(img.getpixel((WIDTH - 1, img.height - 20)), 0)

    def test_receipt_lines_show_item_name_not_item_number(self):
        """receipts print the Item Name above the description, never the QB item number."""
        from sales.printing import Receipt
        drawn = []
        original = Receipt.text
        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        self.item.sku = '6045'
        self.item.name = 'RRF-R15-MAH'
        self.item.save()
        long_item = Item.objects.create(sku='6046', name='BRF-B-S1030-V1-NOUG/BAY SOLD EXTRA LONG NAME', description='Sofa', price=10, on_hand=5, tax_rate=7)
        receipt = save_document(dict(action='complete', customer_id=self.customer.pk,
                                     lines=[dict(item_id=self.item.pk, quantity='2', price='54.95', tax_rate='7'),
                                            dict(item_id=long_item.pk, quantity='1', price='10.00', tax_rate='7')],
                                     payments=[dict(method='cash', amount='200.00')]), 'sale', uuid.uuid4())
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(receipt)
        self.assertIn('RRF-R15-MAH', drawn)
        self.assertNotIn('6045', drawn)
        self.assertNotIn('6046', drawn)
        self.assertTrue(any('BRF-B-S1030-V1-NOUG/BAY' in t for t in drawn))

    def test_star_raster_job_has_proven_command_sequence(self):
        img = render_receipt(self.sale())
        job = star_raster(img)
        self.assertTrue(job.startswith(b'\x1b@\x1b*rR\x1b*rA\x1b*rP0\x00'))
        self.assertTrue(job.endswith(b'\x1b\x0c\x00\x1b*rB'))
        self.assertEqual(len(job), 16 + img.height * (3 + WIDTH // 8) + 7)

    def test_status_decoding(self):
        self.assertEqual(printer_status(READY), '')
        self.assertIn('cover', printer_status(bytes.fromhex('2386200000000000000000')))
        self.assertIn('paper', printer_status(bytes.fromhex('2386000000080000000000')))
        self.assertEqual(printer_status(b''), '')

    def test_print_view_sends_job_to_selected_printer(self):
        receipt = self.sale()
        conn = FakeConnection()
        with mock.patch('sales.printing.socket.create_connection', return_value=conn) as connect:
            response = self.client.post(f'/pos/sale/{receipt.token}/print-receipt/', {'printer': self.printer.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn('TEST Receipt Printer', response.json()['message'])
        connect.assert_called_once_with(('192.0.2.10', 9100), timeout=5)
        self.assertTrue(conn.sent.endswith(b'\x1b\x0c\x00\x1b*rB'))

    def test_print_view_reports_problems_without_changing_the_receipt(self):
        receipt = self.sale()
        version = receipt.version
        with mock.patch('sales.printing.socket.create_connection', side_effect=OSError):
            response = self.client.post(f'/pos/sale/{receipt.token}/print-receipt/', {'printer': self.printer.pk})
        self.assertEqual(response.status_code, 502)
        self.assertIn('Could not reach', response.json()['error'])
        with mock.patch('sales.printing.socket.create_connection', return_value=FakeConnection(bytes.fromhex('2386200000000000000000'))):
            response = self.client.post(f'/pos/sale/{receipt.token}/print-receipt/', {'printer': self.printer.pk})
        self.assertIn('cover is open', response.json()['error'])
        response = self.client.post(f'/pos/sale/{receipt.token}/print-receipt/', {'printer': '999999'})
        self.assertEqual(response.status_code, 502)
        receipt.refresh_from_db()
        self.assertEqual(receipt.version, version)

    def test_held_receipts_and_get_requests_are_refused(self):
        held = self.sale('hold')
        with mock.patch('sales.printing.socket.create_connection') as connect:
            self.assertEqual(self.client.post(f'/pos/sale/{held.token}/print-receipt/', {'printer': self.printer.pk}).status_code, 400)
            self.assertEqual(self.client.get(f'/pos/sale/{self.sale().token}/print-receipt/').status_code, 405)
        connect.assert_not_called()

    def test_completed_receipt_page_offers_receipt_printer(self):
        page = self.client.get(f'/pos/sale/{self.sale().token}/')
        self.assertContains(page, 'id="print-receipt"')
        self.assertContains(page, 'TEST Receipt Printer')

    def actions_bar(self, page):
        html = page.content.decode()
        start = html.index('<div class="actions">')
        return html, html[start:html.index('</div>', start)]

    def test_printer_select_sits_in_the_action_bar_on_new_and_saved_documents(self):
        """one printer select per page, in the bottom action bar left of Cancel, on new and saved receipts/orders."""
        Printer.objects.create(name='Bar Brother', kind='letter', host='192.0.2.31', port=631)
        html, bar = self.actions_bar(self.client.get('/pos/sale/new/'))
        self.assertEqual(html.count('id="receipt-printer"'), 1)
        self.assertIn('data-remember="salePrinter"', bar)
        self.assertIn('data-print-path="print-receipt/"', bar)
        self.assertLess(bar.index('id="receipt-printer"'), bar.index('>Cancel<'))
        self.assertNotIn('id="print-receipt"', bar)  # nothing saved yet to reprint
        self.assertNotIn('id="print-copies"', bar)
        html, bar = self.actions_bar(self.client.get('/pos/order/new/'))
        self.assertIn('data-remember="orderPrinter"', bar)
        self.assertIn('data-print-path="print/"', bar)
        self.assertIn('Bar Brother', bar)
        self.assertNotIn('TEST Receipt Printer', bar)
        self.assertIn('<select id="print-copies" autocomplete="off"><option>1</option><option>2</option><option selected>3</option>', bar)
        receipt = self.sale()
        html, bar = self.actions_bar(self.client.get(f'/pos/sale/{receipt.token}/'))
        self.assertEqual(html.count('id="receipt-printer"'), 1)
        self.assertIn(f'data-url="/pos/sale/{receipt.token}/print-receipt/"', bar)
        self.assertLess(bar.index('id="receipt-printer"'), bar.index('Cancel Receipt'))
        self.assertIn('id="print-document"', html)  # browser Print stays in the sidebar
        order = save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=self.item.pk, quantity='1', price='54.95', tax_rate='7')]), 'order', uuid.uuid4())
        html, bar = self.actions_bar(self.client.get(f'/pos/order/{order.token}/'))
        self.assertEqual(html.count('id="receipt-printer"'), 1)
        self.assertIn(f'data-url="/pos/order/{order.token}/print/"', bar)
        self.assertIn('id="print-copies"', bar)
        held = self.sale('hold')
        html, bar = self.actions_bar(self.client.get(f'/pos/sale/{held.token}/'))
        self.assertIn('id="receipt-printer"', bar)  # for Save & Print
        self.assertNotIn('id="print-receipt"', bar)  # held receipts print through the sidebar control

    def test_save_and_print_sends_directly_instead_of_the_browser_dialog(self):
        """Save & Print posts to the printer endpoint after saving; ?print=1 is only the no-printer fallback."""
        script = self.client.get('/pos/sale/new/').content.decode()
        self.assertIn("sendPrint(result.url+receiptPrinter.dataset.printPath)", script)
        self.assertIn("Saved, but not printed:", script)
        self.assertIn("if(button.dataset.print&&receiptPrinter)", script)
        # The direct send uses the same endpoints as the reprint button; a failed print leaves the receipt saved.
        receipt = self.sale()
        with mock.patch('sales.views.send_receipt', side_effect=PrintError('Printer offline.')):
            response = self.client.post(f'/pos/sale/{receipt.token}/print-receipt/', {'printer': self.printer.pk})
        self.assertEqual((response.status_code, response.json()), (502, {'error': 'Printer offline.'}))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'completed')

    def test_printer_list_edit_status_and_test_print(self):
        page = self.client.get('/printers/')
        self.assertContains(page, 'TEST Receipt Printer')
        self.assertContains(page, f'/printers/{self.printer.pk}/test/')
        self.assertContains(self.client.get('/'), '/printers/')
        with mock.patch('sales.printing.socket.create_connection', return_value=FakeConnection()):
            self.assertEqual(self.client.get(f'/printers/{self.printer.pk}/status/').json(), {'ok': True, 'status': 'Ready'})
        with mock.patch('sales.printing.socket.create_connection', side_effect=OSError):
            self.assertEqual(self.client.get(f'/printers/{self.printer.pk}/status/').json()['status'], 'Not reachable')
        conn = FakeConnection()
        with mock.patch('sales.printing.socket.create_connection', return_value=conn):
            response = self.client.post(f'/printers/{self.printer.pk}/test/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(conn.sent.endswith(b'\x1b\x0c\x00\x1b*rB'))
        self.assertEqual(render_test_page(self.printer).width, WIDTH)

    def test_add_and_edit_printer_with_validation(self):
        data = dict(name='TEST Other Receipt Printer', kind='receipt', model='Star mC-Print3', location='Rear counter (West)', host='192.0.2.20', port='9100', mac_address='00-00-5e-00-53-df', serial_number='', notes='', active='on')
        self.assertEqual(self.client.post('/printers/new/', data).status_code, 302)
        west = Printer.objects.get(name='TEST Other Receipt Printer')
        self.assertEqual((west.mac_address, west.version), ('00:00:5E:00:53:DF', 1))
        bad = self.client.post('/printers/new/', dict(data, name='TEST Receipt Printer', host='bad host;', mac_address='zz'))
        self.assertEqual(bad.status_code, 400)
        self.assertContains(bad, 'IP address such as', status_code=400)
        self.assertContains(bad, 'MAC address such as', status_code=400)
        self.assertContains(bad, 'already', status_code=400)
        stale = self.client.post(f'/printers/{west.pk}/', dict(data, version='0', location='Moved'))
        self.assertContains(stale, 'Someone changed this printer', status_code=400)
        self.assertEqual(self.client.post(f'/printers/{west.pk}/', dict(data, version='1', active='')).status_code, 302)
        west.refresh_from_db()
        self.assertFalse(west.active)
        self.assertNotContains(self.client.get(f'/pos/sale/{self.sale().token}/'), 'TEST Other Receipt Printer')
        with mock.patch('sales.printing.socket.create_connection') as connect:
            self.assertEqual(self.client.post(f'/printers/{west.pk}/test/').status_code, 502)
        connect.assert_not_called()


def decode_pwg(data):
    """Independent decoder used to check the encoder: returns a list of (header, rows)."""
    import struct
    assert data[:4] == b'RaS2'
    i, pages = 4, []
    while i < len(data):
        header = data[i:i + 1796]
        i += 1796
        width, height = struct.unpack('>II', header[372:380])
        rows = []
        while len(rows) < height:
            repeat = data[i]
            i += 1
            row = bytearray()
            while len(row) < width:
                c = data[i]
                i += 1
                if c == 128:
                    row += b'\xff' * (width - len(row))
                elif c < 128:
                    row += bytes([data[i]]) * (c + 1)
                    i += 1
                else:
                    count = 257 - c
                    row += data[i:i + count]
                    i += count
            rows += [bytes(row)] * (repeat + 1)
        pages.append((header, rows))
    return pages


class FakeIPP:
    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.reply


def ipp_reply(status=0, attributes=b''):
    import struct
    return struct.pack('>BBHI', 2, 0, status, 1) + b'\x01' + attributes + b'\x03'


class SalesOrderPrinting(TestCase):
    def setUp(self):
        from .letter_printing import ipp_attribute
        self.ipp_attribute = ipp_attribute
        self.customer = Customer.objects.create(source_id='order-print', full_name='Pat Buyer', phone='518-555-0100', email='pat@example.com')
        self.item = Item.objects.create(sku='FSF-FS-1067', name='Hall seat', description='Custom hall seat', price=1629.95, on_hand=0, tax_rate=7)
        self.brother = Printer.objects.create(name='Test Brother', kind='letter', host='192.0.2.30', port=631)
        Printer.objects.create(name='TEST Receipt Printer', kind='receipt', host='192.0.2.10', port=9100)

    def order(self, lines=1, description='CUSTOM BASKET TOP HALL SEAT.\n42"W X 15.5"D X 78"H.\nRED OAK.'):
        from .services import save_order_payment
        order = save_document(dict(action='save', customer_id=self.customer.pk, notes='TBO\nAPPX 16 WEEKS',
                                   lines=[dict(item_id=self.item.pk, quantity='1', price='1629.95', tax_rate='7', description=description)] * lines), 'order', uuid.uuid4())
        save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount='436.00', method='credit', reference='Deposit'))
        order.refresh_from_db()
        return order

    def test_pwg_encoder_round_trips_rendered_pages(self):
        from .letter_printing import PAGE_H, PAGE_W, pwg_raster, render_sales_order
        pages = render_sales_order(self.order())
        self.assertEqual(len(pages), 1)
        decoded = decode_pwg(pwg_raster(pages))
        self.assertEqual(len(decoded), 1)
        header, rows = decoded[0]
        self.assertEqual(header[:9], b'PwgRaster')
        self.assertEqual((len(rows), len(rows[0])), (PAGE_H, PAGE_W))
        self.assertEqual(b''.join(rows), pages[0].tobytes())

    def test_long_orders_continue_on_more_pages(self):
        from .letter_printing import pwg_raster, render_sales_order
        pages = render_sales_order(self.order(lines=40))
        self.assertGreater(len(pages), 1)
        self.assertEqual(len(decode_pwg(pwg_raster(pages))), len(pages))

    def test_order_printout_has_no_sold_or_due_columns_and_names_tax_location(self):
        # Sold/Due columns dropped (description gets the width); 'Springfield' sits on the tax line.
        from .letter_printing import Pages, render_sales_order
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(self.order(lines=40, description='WIDE ' * 60))
        self.assertGreater(len(pages), 1)
        for steps in pages:
            texts = [step[3] for step in steps if step[0] == 'text']
            self.assertIn('Item Description', texts)
            self.assertIn('Qty', texts)
            self.assertNotIn('Sold', texts)
            self.assertNotIn('Due', texts)
        texts = [step[3] for steps in pages for step in steps if step[0] == 'text']
        self.assertIn('Springfield 7 % Tax:', texts)
        self.assertNotIn('Springfield', texts)
        self.assertNotIn('7 % Tax:', texts)
        # 300 characters wrap within the widened column: 'WIDE ' x 60 fits in fewer lines than at the old 700 px width.
        wraps = [step for step in pages[0] if step[0] == 'text' and step[3].startswith('WIDE')]
        self.assertTrue(all(step[1] + Pages().width(step[3], 36) < 1873 for step in wraps))
        self.assertLess(len(Pages().lines('WIDE ' * 60, 36, 1840 - 860)), len(Pages().lines('WIDE ' * 60, 36, 1560 - 860)))

    def test_order_print_sends_ipp_job_with_copies(self):
        # the Brothers print PWG raster exactly once whatever the IPP 'copies' says, so 3 copies = the page set
        # three times in one raster stream, with copies=1 on the job.
        order = self.order()
        sent = {}

        def urlopen(request, timeout):
            sent['url'], sent['body'] = request.full_url, request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            response = self.client.post(f'/pos/order/{order.token}/print/', {'printer': self.brother.pk, 'copies': '3'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('3 copies', response.json()['message'])
        self.assertEqual(sent['url'], 'http://192.0.2.30:631/ipp/print')
        self.assertIn(b'image/pwg-raster', sent['body'])
        self.assertIn(b'\x21\x00\x06copies\x00\x04\x00\x00\x00\x01', sent['body'])
        self.assertNotIn(b'\x21\x00\x06copies\x00\x04\x00\x00\x00\x03', sent['body'])
        raster = sent['body'][sent['body'].index(b'RaS2'):]
        pages = decode_pwg(raster)
        self.assertEqual(len(pages), 3)
        headers = [header for header, _ in pages]
        self.assertTrue(all(header[340:344] == b'\x00\x00\x00\x01' for header in headers))  # NumCopies stays 1
        self.assertTrue(all(header[452:456] == b'\x00\x00\x00\x03' for header in headers))  # TotalPageCount counts the copies
        self.assertEqual(pages[0][1], pages[1][1])
        self.assertEqual(pages[1][1], pages[2][1])

    def test_one_copy_sends_the_pages_once_and_copies_collate(self):
        from .letter_printing import copy_sets, pwg_raster, render_sales_order
        order = self.order()
        sent = {}

        def urlopen(request, timeout):
            sent['body'] = request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            self.assertEqual(self.client.post(f'/pos/order/{order.token}/print/', {'printer': self.brother.pk, 'copies': '1'}).status_code, 200)
        self.assertEqual(len(decode_pwg(sent['body'][sent['body'].index(b'RaS2'):])), 1)
        # A multi-page order repeats as collated sets: 1,2,1,2 - never 1,1,2,2.
        pages = render_sales_order(self.order(lines=40))
        self.assertGreater(len(pages), 1)
        sets = copy_sets(pages, 2)
        self.assertEqual(len(sets), 2 * len(pages))
        self.assertEqual([id(page) for page in sets], [id(page) for page in pages] * 2)
        self.assertEqual(len(decode_pwg(pwg_raster(copy_sets(pages[:1], 3)))), 3)

    def page_texts(self, order, **settings):
        from .letter_printing import Pages, render_sales_order
        with override_settings(**settings), mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(order)
        return [[step[3] for step in steps if step[0] == 'text'] for steps in pages]

    def test_short_orders_print_on_one_page_and_the_footer_stays_with_the_totals(self):
        # a short order printed on 2 pages with most of page 1 empty. The closing block reserved
        # room for 7 payment rows + 500 px whatever the order held; now it reserves what it really needs.
        from .letter_printing import Pages, closing_height, render_sales_order
        from .services import save_order_payment
        for payments in (0, 2):
            order = self.order(lines=3)
            order.payments.all().delete()
            for _ in range(payments):
                order.refresh_from_db()
                save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount='100.00', method='cash', reference='Deposit'))
            order.refresh_from_db()
            texts = self.page_texts(order)
            self.assertEqual(len(texts), 1, (payments, texts))
            self.assertEqual(texts[0].count('Deposit History'), 1 if payments else 0)
            self.assertIn('Thank you for your order!', texts[0])
            self.assertIn(STORE_BLOCK_LAST, texts[0])
        # 7 and 8 three-line items fit on one page without testing marks (production); 6 was the old limit.
        self.assertEqual(len(self.page_texts(self.order(lines=8), PRINT_TESTING_MARKS=False)), 1)
        # A genuinely long order still breaks, and the whole closing block sits on the last page, never alone:
        # the last page has line rows (an Ext Price) above TOTAL and the store block, with no footer orphaned.
        texts = self.page_texts(self.order(lines=40))
        self.assertGreaterEqual(len(texts), 2)
        last = texts[-1]
        for label in ('Subtotal:', 'TOTAL:', 'Balance Due:', 'Thank you for your order!', STORE_BLOCK_LAST):
            self.assertIn(label, last)
            self.assertTrue(all(label not in page for page in texts[:-1]), label)
        self.assertIn('$1,629.95', last)  # line rows on the closing page too: the block follows the table
        # The reserved height is what the block draws: a 3-line order's block measured against its own steps.
        order = self.order(lines=3)
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            steps = render_sales_order(order)[0]
        ys = [step[2] for step in steps if step[0] == 'text']
        texts = [step[3] for step in steps if step[0] == 'text']
        start = ys[texts.index('Subtotal:')]
        end = ys[texts.index(STORE_BLOCK_LAST)]
        p = Pages()
        totals = [('', '', False)] * 5
        self.assertEqual(closing_height(p, list(order.payments.all()), totals, Decimal('0')), (end - start) + 40 + int(40 * 1.25))

    def test_three_copies_of_a_short_order_are_three_pages(self):
        from .letter_printing import copy_sets, render_sales_order
        pages = render_sales_order(self.order(lines=3))
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(copy_sets(pages, 3)), 3)

    def info_block(self, order):
        """Text lines of the Customer Info column (left of the NOTES column) on the first page."""
        from .letter_printing import Pages, render_sales_order
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            steps = render_sales_order(order)[0]
        texts = [(step[2], step[1], step[3]) for step in steps if step[0] == 'text']
        start = next(y for y, x, text in texts if text == 'Customer Info')
        end = next(y for y, x, text in texts if text.startswith('Order Status:'))
        return [text for y, x, text in sorted(texts) if start < y < end and x < 1100]

    def test_customer_info_prints_the_full_address(self):
        # the address never printed - the block only read name, phone, mobile and email.
        self.customer.street = '12 Bay Road'
        self.customer.city, self.customer.state, self.customer.postal_code = 'Springfield', 'NY', '62701'
        self.customer.mobile = '518-555-0199'
        self.customer.save()
        self.assertEqual(self.info_block(self.order()), ['Pat Buyer', '12 Bay Road', 'Springfield NY 62701', '518-555-0100', '518-555-0199', 'pat@example.com'])

    def test_customer_info_with_partial_address_company_and_two_street_lines(self):
        self.customer.street = 'Lake George Lodge\nPO Box 44'
        self.customer.city, self.customer.state, self.customer.postal_code = 'Lake George', 'NY', ''
        self.customer.company = 'LG Lodge LLC'
        self.customer.save()
        self.assertEqual(self.info_block(self.order()), ['Pat Buyer', 'LG Lodge LLC', 'Lake George Lodge', 'PO Box 44', 'Lake George NY', '518-555-0100', 'pat@example.com'])
        self.customer.street, self.customer.city, self.customer.state, self.customer.company = '', '', '', ''
        self.customer.postal_code = '12845'
        self.customer.save()
        self.assertEqual(self.info_block(self.order()), ['Pat Buyer', '12845', '518-555-0100', 'pat@example.com'])

    def test_customer_info_without_an_address_matches_the_qb_sample(self):
        # QB sample #5367: a customer with no address prints name, phone, email only - no blank lines, no 'Ship To'.
        self.assertEqual(self.info_block(self.order()), ['Pat Buyer', '518-555-0100', 'pat@example.com'])

    def test_ship_to_prints_only_when_it_differs_from_the_customer_address(self):
        from .letter_printing import ship_to_lines
        self.customer.street, self.customer.city, self.customer.state, self.customer.postal_code = '12 Bay Road', 'Springfield', 'NY', '62701'
        self.customer.save()
        order = self.order()
        order.shipping_address = 'Pat Buyer\n12 Bay Road\nSpringfield NY 62701'  # the default the editor fills in
        order.save()
        self.assertEqual(ship_to_lines(order), [])
        self.assertNotIn('Ship To', self.info_block(order))
        order.shipping_address = 'Camp Pat\n9 Lakeshore Dr\nBolton Landing NY 12814'
        order.save()
        self.assertEqual(ship_to_lines(order), ['Camp Pat', '9 Lakeshore Dr', 'Bolton Landing NY 12814'])
        block = self.info_block(order)
        self.assertEqual(block[block.index('Ship To'):], ['Ship To', 'Camp Pat', '9 Lakeshore Dr', 'Bolton Landing NY 12814'])
        self.assertEqual(block[:3], ['Pat Buyer', '12 Bay Road', 'Springfield NY 62701'])

    def test_order_print_validation_and_refusal(self):
        order = self.order()
        star = Printer.objects.get(name='TEST Receipt Printer')
        with mock.patch('sales.letter_printing.urllib.request.urlopen') as urlopen:
            self.assertEqual(self.client.post(f'/pos/order/{order.token}/print/', {'printer': star.pk}).status_code, 400)
            self.assertEqual(self.client.post(f'/pos/order/{order.token}/print/', {'printer': self.brother.pk, 'copies': '9'}).status_code, 400)
        urlopen.assert_not_called()
        with mock.patch('sales.letter_printing.urllib.request.urlopen', return_value=FakeIPP(ipp_reply(0x0400))):
            response = self.client.post(f'/pos/order/{order.token}/print/', {'printer': self.brother.pk, 'copies': '1'})
        self.assertEqual(response.status_code, 502)
        self.assertIn('refused', response.json()['error'])
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=OSError):
            self.assertIn('Could not reach', self.client.post(f'/pos/order/{order.token}/print/', {'printer': self.brother.pk, 'copies': '1'}).json()['error'])

    def test_letter_status_and_test_page(self):
        import struct
        ready = ipp_reply(0, self.ipp_attribute(0x23, 'printer-state', struct.pack('>i', 3)) + self.ipp_attribute(0x44, 'printer-state-reasons', 'none'))
        jam = ipp_reply(0, self.ipp_attribute(0x23, 'printer-state', struct.pack('>i', 5)) + self.ipp_attribute(0x44, 'printer-state-reasons', 'media-jam-error'))
        with mock.patch('sales.letter_printing.urllib.request.urlopen', return_value=FakeIPP(ready)):
            self.assertEqual(self.client.get(f'/printers/{self.brother.pk}/status/').json(), {'ok': True, 'status': 'Ready'})
        with mock.patch('sales.letter_printing.urllib.request.urlopen', return_value=FakeIPP(jam)):
            self.assertEqual(self.client.get(f'/printers/{self.brother.pk}/status/').json(), {'ok': False, 'status': 'Paper jam'})
        with mock.patch('sales.letter_printing.urllib.request.urlopen', return_value=FakeIPP(ipp_reply())):
            self.assertEqual(self.client.post(f'/printers/{self.brother.pk}/test/').status_code, 200)

    def test_order_page_offers_letter_printers_only(self):
        page = self.client.get(f'/pos/order/{self.order().token}/')
        self.assertContains(page, 'Print Sales Order')
        self.assertContains(page, 'Test Brother')
        self.assertNotContains(page, 'TEST Receipt Printer')

    def test_inactive_printers_are_never_contacted(self):
        star = Printer.objects.get(name='TEST Receipt Printer')
        Printer.objects.filter(pk__in=[star.pk, self.brother.pk]).update(active=False, host='192.0.2.99')
        with mock.patch('sales.printing.socket.create_connection') as connect, mock.patch('sales.letter_printing.urllib.request.urlopen') as urlopen:
            page = self.client.get('/printers/?status=all')
            for printer in (star, self.brother):
                self.assertEqual(self.client.get(f'/printers/{printer.pk}/status/').json(), {'ok': False, 'status': 'Inactive (not checked)'})
                self.assertEqual(self.client.post(f'/printers/{printer.pk}/test/').status_code, 502)
                self.assertNotContains(page, f'/printers/{printer.pk}/status/')
                self.assertNotContains(page, f'/printers/{printer.pk}/test/')
        connect.assert_not_called()
        urlopen.assert_not_called()
        self.assertContains(page, 'Inactive (not checked)')


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class PrintControl(TestCase):
    """The shared printer picker: suited printers + Create PDF, one print path, choice remembered per session."""

    def setUp(self):
        from decimal import Decimal
        from django.utils import timezone
        from .models import Document, Line, Tender
        self.star = Printer.objects.create(name='TEST Receipt Printer', kind='receipt', host='192.0.2.10', port=9100)
        self.brother = Printer.objects.create(name='Control Brother', kind='letter', host='192.0.2.40', port=631)
        self.record = Document.objects.create(kind='sale', status='completed', was_completed=True, source_id='0123456789abcdef', source_number=9,
                                              receipt_type=0, customer_name='Imported buyer', completed_at=timezone.now(), subtotal='10.00', tax='0.70', total='10.70', cashier='Morgan')
        Line.objects.create(document=self.record, source_id='0123456789abcdef:0', position=0, sku='123', item_name='Chair', description='Oak chair', quantity=1, price='10.00', tax_rate=7)
        Tender.objects.create(document=self.record, position=0, method='cash', amount=Decimal('10.70'), tender_name='Cash')
        self.record.detail_url = f'/pos/sale/{self.record.token}/'
        self.url = f'/print/sale/{self.record.token}/'

    def test_imported_receipt_page_lists_receipt_and_letter_printers_and_pdf(self):
        page = self.client.get(self.record.detail_url)
        self.assertContains(page, 'class="print-control"')
        self.assertContains(page, '<optgroup label="Receipt printers">')
        self.assertContains(page, '<optgroup label="Letter printers">')
        self.assertContains(page, 'Create PDF')
        self.assertContains(page, f'data-print-url="{self.url}"')
        self.assertContains(page, 'Imported from QuickBooks POS (9)')
        self.assertContains(page, 'Cash: $10.70')

    def test_print_to_star_and_letter_and_remember_choice(self):
        conn = FakeConnection()
        with mock.patch('sales.printing.socket.create_connection', return_value=conn):
            response = self.client.post(self.url, {'target': self.star.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(conn.sent.endswith(b'\x1b\x0c\x00\x1b*rB'))
        self.assertEqual(self.client.session['print_targets'], {'receipt': str(self.star.pk)})
        sent = {}

        def urlopen(request, timeout):
            sent['body'] = request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            self.assertEqual(self.client.post(self.url, {'target': self.brother.pk}).status_code, 200)
        self.assertIn(b'image/pwg-raster', sent['body'])
        page = self.client.get(self.record.detail_url)
        self.assertContains(page, f'<option value="{self.brother.pk}" selected>')

    def test_pdf_and_choice_endpoint(self):
        response = self.client.get(f'{self.url}pdf/')
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn('inline; filename="Sales-Receipt-9.pdf"', response['Content-Disposition'])
        self.assertEqual(self.client.post('/print/choice/', {'kind': 'receipt', 'target': 'pdf'}).json(), {'ok': True})
        self.assertContains(self.client.get(self.record.detail_url), '<option value="pdf" selected>')
        self.assertEqual(self.client.post('/print/choice/', {'kind': 'receipt', 'target': '999999'}).status_code, 400)

    def test_rejects_unsuited_inactive_unknown_and_unprintable(self):
        with mock.patch('sales.printing.socket.create_connection') as connect, mock.patch('sales.letter_printing.urllib.request.urlopen') as urlopen:
            Printer.objects.filter(pk=self.brother.pk).update(active=False)
            self.assertEqual(self.client.post(self.url, {'target': self.brother.pk}).status_code, 502)
            self.assertEqual(self.client.post(self.url, {'target': 'bogus'}).status_code, 502)
            self.assertEqual(self.client.post(f'/print/nope/{self.record.token}/', {'target': self.star.pk}).status_code, 404)
            self.record.status = 'held'  # QB Held Receipts > Reprint: held receipts print, stamped HELD
            self.record.save()
            self.assertEqual(self.client.get(f'{self.url}pdf/').status_code, 200)
            self.assertEqual(self.client.get(f'/print/order/{self.record.token}/pdf/').status_code, 404)  # a receipt is not an order
            self.assertEqual(self.client.get(self.url).status_code, 405)
        connect.assert_not_called()
        urlopen.assert_not_called()

    def test_sales_orders_offer_letter_printers_and_pdf_only(self):
        from .print_documents import targets
        self.assertEqual([label for label, _ in targets('order')], ['Letter printers'])
        self.assertEqual([label for label, _ in targets('receipt')], ['Receipt printers', 'Letter printers'])
