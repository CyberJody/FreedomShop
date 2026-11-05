# SPDX-License-Identifier: AGPL-3.0-or-later
"""PO item labels, one QR label per piece ordered, SheetLabels SL548."""
import ctypes
import ctypes.util
import uuid
from decimal import Decimal
from unittest import mock

from django.test import TestCase, SimpleTestCase, override_settings

from sales.models import Item, Printer
from sales.services import WorkflowError
from . import piece_labels
from .models import PieceLabel, PurchaseDocument, Vendor
from .piece_codes import ALPHABET, new_code, normalize, parse_piece_code, piece_url
from .services import save_purchase

PT = 0.5   # geometry tolerance, points


def header(vendor, token=None, version=0):
    return dict(token=token or uuid.uuid4(), version=version, vendor=vendor, status='open', notes='', terms='',
                ship_date=None, payment_due=None, invoice_number='')


def pages(pdf):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def zbar_decode(image):
    """QR contents found in a PIL image, via the system's libzbar (ctypes; no Python package needed)."""
    lib = ctypes.CDLL(ctypes.util.find_library('zbar') or 'libzbar.so.0')
    vp, ul, ui = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint
    lib.zbar_image_scanner_create.restype = vp
    lib.zbar_image_scanner_set_config.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.zbar_image_create.restype = vp
    lib.zbar_image_set_format.argtypes = [vp, ul]
    lib.zbar_image_set_size.argtypes = [vp, ui, ui]
    lib.zbar_image_set_data.argtypes = [vp, ctypes.c_char_p, ul, vp]
    lib.zbar_scan_image.argtypes = [vp, vp]
    lib.zbar_image_first_symbol.argtypes = [vp]
    lib.zbar_image_first_symbol.restype = vp
    lib.zbar_symbol_next.argtypes = [vp]
    lib.zbar_symbol_next.restype = vp
    lib.zbar_symbol_get_data.argtypes = [vp]
    lib.zbar_symbol_get_data.restype = ctypes.c_char_p
    lib.zbar_image_destroy.argtypes = [vp]
    lib.zbar_image_scanner_destroy.argtypes = [vp]
    gray = image.convert('L')
    data = gray.tobytes()
    scanner = lib.zbar_image_scanner_create()
    lib.zbar_image_scanner_set_config(scanner, 0, 0, 0)    # all symbologies off (text can read as a 1-D barcode)...
    lib.zbar_image_scanner_set_config(scanner, 64, 0, 1)   #... then QR (ZBAR_QRCODE) on
    img = lib.zbar_image_create()
    found = []
    try:
        lib.zbar_image_set_format(img, int.from_bytes(b'Y800', 'little'))
        lib.zbar_image_set_size(img, gray.width, gray.height)
        lib.zbar_image_set_data(img, data, len(data), None)
        lib.zbar_scan_image(scanner, img)
        symbol = lib.zbar_image_first_symbol(img)
        while symbol:
            found.append(lib.zbar_symbol_get_data(symbol).decode())
            symbol = lib.zbar_symbol_next(symbol)
    finally:
        lib.zbar_image_destroy(img)
        lib.zbar_image_scanner_destroy(scanner)
    return found


def have_zbar():
    try:
        ctypes.CDLL(ctypes.util.find_library('zbar') or 'libzbar.so.0')
        return True
    except OSError:
        return False


class PieceCodeTests(SimpleTestCase):
    def test_codes_are_short_unambiguous_and_self_checking(self):
        codes = {new_code() for _ in range(500)}
        self.assertEqual(len(codes), 500)
        for code in list(codes)[:50]:
            self.assertRegex(code, r'^FS-[2-9A-Z]{4}-[2-9A-Z]{4}$')
            self.assertFalse(set(code[3:].replace('-', '')) & set('01OILU'))
            self.assertEqual(normalize(code), code)
        code = 'WC-' + next(iter(codes))[3:]
        chars = code.replace('-', '')[2:]
        for position in range(len(chars)):   # any single wrong character is caught
            for other in ALPHABET:
                if other != chars[position]:
                    bad = chars[:position] + other + chars[position + 1:]
                    self.assertIsNone(normalize('WC' + bad), (code, position, other))

    def test_parser_takes_the_bare_code_or_the_label_url(self):
        code = new_code()
        self.assertEqual(parse_piece_code(code), code)
        self.assertEqual(parse_piece_code(code.lower()), code)
        self.assertEqual(parse_piece_code(code.replace('-', '')), code)
        self.assertEqual(parse_piece_code(f'  {code}\r\n'), code)            # scanner Enter suffix
        self.assertEqual(parse_piece_code(piece_url(code)), code)
        self.assertEqual(parse_piece_code(piece_url(code) + '\n'), code)
        self.assertEqual(parse_piece_code(f'https://shop.example.com/shop/item?utm_source=label&wc={code.lower()}&x=1'), code)
        self.assertEqual(parse_piece_code(f'http://example.org/a/b/?WC={code}'), code)
        for junk in ['', '   ', None, 'WC-', '6045', 'RRF-R15-MAH', 'https://shop.example.com/', 'https://shop.example.com/?wc=',
                     'https://shop.example.com/?wc=6045', code[:-1] + ('2' if code[-1] != '2' else '3'), 'WC-OOOO-IIII']:
            self.assertIsNone(parse_piece_code(junk), junk)

    def test_label_url_is_one_setting(self):
        self.assertEqual(piece_url('WC-ABCD-EFGH'), 'https://example.com/?wc=WC-ABCD-EFGH')
        # The parser accepts any host or path carrying a wc= parameter, including older label addresses.
        code = new_code()
        self.assertEqual(parse_piece_code(f'https://example.com/?wc={code}'), code)
        self.assertEqual(parse_piece_code(f'https://labels.example.org/?wc={code}'), code)
        with override_settings(PIECE_LABEL_URL='https://shop.example.com/p?id={code}&src=label'):
            self.assertEqual(piece_url('WC-ABCD-EFGH'), 'https://shop.example.com/p?id=WC-ABCD-EFGH&src=label')
        with override_settings(PIECE_LABEL_URL='https://shop.example.com/scan'):
            url = piece_url('WC-ABCD-EFGH')
            self.assertEqual(url, 'https://shop.example.com/scan?wc=WC-ABCD-EFGH')
        code = new_code()
        with override_settings(PIECE_LABEL_URL='https://shop.example.com/scan'):
            self.assertEqual(parse_piece_code(piece_url(code)), code)


class GeometryTests(SimpleTestCase):
    """SL548 (sheetlabels.com spec + template): 3 x 5, 2.688" x 2"."""
    def test_label_origins_sit_on_the_sl548_grid(self):
        lefts = [0.15625, 2.90625, 5.65625]
        tops = [0.25, 2.375, 4.5, 6.625, 8.75]
        for index in range(15):
            x, y = piece_labels.origin(index)
            self.assertAlmostEqual(x, lefts[index % 3] * 72, delta=PT)
            self.assertAlmostEqual(792 - (y + piece_labels.LABEL_H), tops[index // 3] * 72, delta=PT)
        self.assertAlmostEqual(piece_labels.LABEL_W, 2.688 * 72, delta=PT)
        self.assertAlmostEqual(piece_labels.LABEL_H, 144, delta=PT)
        right_edge = piece_labels.origin(2)[0] + piece_labels.LABEL_W
        self.assertAlmostEqual(612 - right_edge, 0.15625 * 72, delta=PT)
        self.assertAlmostEqual(piece_labels.origin(14)[1], 0.25 * 72, delta=PT)   # bottom margin

    def label(self, n=1, **kw):
        data = dict(name='RRF-R15-MAH', description='Rake-back side chair, OCS stain', quantity=n, piece=1,
                    po_number='PO-8184', code='WC-ABCD-EFGH', url='https://example.com/?wc=WC-ABCD-EFGH')
        data.update(kw)
        return data

    def test_sixteen_labels_fill_one_sheet_then_start_the_next_at_the_top_left(self):
        seen = []
        real = piece_labels.draw_label
        with mock.patch.object(piece_labels, 'draw_label', side_effect=lambda c, label, x, y, marks=False: (seen.append((x, y)), real(c, label, x, y, marks))):
            pdf = piece_labels.pdf_bytes([self.label()] * 16)
        self.assertEqual(pages(pdf), 2)
        self.assertEqual(seen[:15], [piece_labels.origin(i) for i in range(15)])
        self.assertEqual(seen[15], piece_labels.origin(0))
        self.assertEqual(pages(piece_labels.pdf_bytes([self.label()] * 8)), 1)
        self.assertEqual(pages(piece_labels.pdf_bytes([self.label()] * 8, copies=2)), 2)   # 16 labels
        self.assertEqual(pages(piece_labels.pdf_bytes([self.label()] * 15)), 1)
        self.assertEqual(pages(piece_labels.alignment_pdf()), 1)

    def test_text_stays_readable_and_long_text_is_cut(self):
        size, lines = piece_labels.name_layout('Amish Mission Rake-Back Upholstered Side Chair XL Extra', 97)
        self.assertGreaterEqual(size, 12)
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith('...'))
        size, lines = piece_labels.name_layout('RRF-R15-MAH', 97)
        self.assertEqual((size, lines), (14, ['RRF-R15-MAH']))
        self.assertGreaterEqual(piece_labels.DESCRIPTION_MIN_SIZE, 9)
        lines = piece_labels.fit('word ' * 200, 'Helvetica', 10, 182, 3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith('...'))
        self.assertEqual(piece_labels.wrap('ABCDEFGHIJKLMNOPQRSTUVWXYZ' * 3, 'Helvetica-Bold', 12, 97)[0][:3], 'ABC')  # long words break
        self.assertEqual(piece_labels.plain('Oak \u2013 42\u201d'), 'Oak - 42"')   # printed text is plain ASCII

    def test_description_gets_the_room_and_no_serial_or_item_text(self):
        """The description and 'Piece N of M' fit below the QR. No serial or item number in
        text (the QR carries the serial); the description takes the space under the QR, shrinking to 9 pt before it is
        cut. The long chair description from the store fits whole."""
        import pypdfium2 as pdfium
        chair = '(SOLD 5400 HERMANCE) HICKORY SIDE CHAIRS W/BAY SEAT (BUCKEYE 1065-B) AND SPINDLE BACK'
        labels = [self.label(description=chair, item_number='17027'),
                  self.label(code='WC-ABCD-EFGJ', description='word ' * 80, item_number='BRF-B-1000-H-N')]
        doc = pdfium.PdfDocument(piece_labels.pdf_bytes(labels))
        text = doc[0].get_textpage().get_text_range()
        doc.close()
        self.assertNotIn('Serial', text)
        self.assertNotIn('WC-ABCD-EFGH', text)            # the serial is only in the QR
        self.assertNotIn('Item:', text)
        self.assertNotIn('17027', text)
        self.assertIn('SPINDLE BACK', text)                # the whole chair description fits
        self.assertIn('...', text)                         # the 80-word one is cut
        self.assertIn('Piece 1 of 1', text)
        self.assertIn('PO Quantity: 1', text)
        self.assertGreaterEqual(piece_labels.DESCRIPTION_MIN_SIZE, 9)

class LabelBase(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='AMISH', company='Sample Builder')
        self.chair = Item.objects.create(sku='6045', name='RRF-R15-MAH', description='Rake-back side chair',
                                         price=300, on_hand=0, average_cost=0, order_cost=100, primary_vendor=self.vendor, tax_rate=7)
        self.table = Item.objects.create(sku='6046', name='RRF-T42-MAH', description='Oak table 42x60, 2 leaves',
                                         price=900, on_hand=0, average_cost=0, order_cost=400, primary_vendor=self.vendor, tax_rate=7)
        self.po = self.save([(self.chair, 6), (self.table, 2)])

    def save(self, rows, po=None):
        lines = list(po.lines.all()) if po else []
        data = []
        for index, (item, quantity) in enumerate(rows):
            row = dict(item=item, quantity=Decimal(quantity), cost=Decimal('100'))
            match = next((l for l in lines if l.item_id == item.pk), None)
            if match:
                row['line_id'] = match.pk
            data.append(row)
        return save_purchase(header(self.vendor, po.token if po else None, po.version if po else 0), data, 'po', 'save')


class PieceCodeStorageTests(LabelBase):
    def test_one_code_per_piece_made_once_and_reprints_match(self):
        self.assertEqual(PieceLabel.objects.count(), 0)          # nothing is written until labels are printed
        self.assertEqual(piece_labels.label_count(self.po), 8)
        labels = piece_labels.labels_for_print(self.po)
        self.assertEqual(len(labels), 8)
        self.assertEqual(PieceLabel.objects.count(), 8)
        self.assertEqual(len({l['code'] for l in labels}), 8)
        # Standard rule 3: every label carries the item number in plain text; a line with no item prints (none).
        self.assertEqual({l['item_number'] for l in labels}, {self.chair.sku, self.table.sku})
        self.assertNotIn('(none)', {l['item_number'] for l in labels})
        self.assertEqual([(l['name'], l['piece'], l['quantity']) for l in labels][:2], [('RRF-R15-MAH', 1, 6), ('RRF-R15-MAH', 2, 6)])
        self.assertEqual(labels[6]['name'], 'RRF-T42-MAH')
        self.assertEqual(labels[0]['po_number'], self.po.number)
        again = piece_labels.labels_for_print(PurchaseDocument.objects.get(pk=self.po.pk))
        self.assertEqual(again, labels)
        self.assertEqual(PieceLabel.objects.count(), 8)

    def test_more_pieces_get_new_codes_and_fewer_keep_the_old_ones(self):
        first = {l['code'] for l in piece_labels.labels_for_print(self.po)}
        po = self.save([(self.chair, 7), (self.table, 2)], self.po)
        grown = piece_labels.labels_for_print(po)
        self.assertEqual(len(grown), 9)
        self.assertTrue(first <= {l['code'] for l in grown})
        po = self.save([(self.chair, 3), (self.table, 2)], po)
        shrunk = piece_labels.labels_for_print(po)
        self.assertEqual(len(shrunk), 5)
        self.assertEqual(PieceLabel.objects.count(), 9)          # never deleted or renumbered
        self.assertEqual(shrunk[0]['code'], grown[0]['code'])
        self.assertEqual(shrunk[0]['quantity'], 3)

    def test_a_labelled_line_cannot_be_taken_off_the_po(self):
        piece_labels.labels_for_print(self.po)
        with self.assertRaisesRegex(WorkflowError, 'already has item labels printed'):
            self.save([(self.chair, 6)], self.po)
        self.assertEqual(self.po.lines.count(), 2)
        other = self.save([(self.chair, 1), (self.table, 1)])
        self.assertEqual(self.save([(self.chair, 1)], other).lines.count(), 1)   # unlabelled POs edit as before

    def test_refusals_are_plain_and_write_nothing(self):
        empty = PurchaseDocument.objects.create(token=uuid.uuid4(), kind='po', status='open', vendor=self.vendor,
                                                vendor_name='Sample Builder', total=0)
        with self.assertRaisesRegex(piece_labels.LabelError, 'no items with a quantity'):
            piece_labels.labels_for_print(empty)
        big = self.save([(self.chair, 400), (self.table, 51)])
        with self.assertRaisesRegex(piece_labels.LabelError, '451 labels'):
            piece_labels.labels_for_print(big)
        with self.assertRaisesRegex(piece_labels.LabelError, '480 labels'):
            piece_labels.labels_for_print(self.save([(self.chair, 240)]), copies=2)
        self.assertEqual(PieceLabel.objects.count(), 0)

    def test_the_qr_scans_back_to_the_url_and_the_code(self):
        if not have_zbar():
            self.skipTest('libzbar not installed; QR content is still checked through segno below')
        import pypdfium2 as pdfium
        labels = piece_labels.labels_for_print(self.po)
        pdf = piece_labels.pdf_bytes(labels)
        document = pdfium.PdfDocument(pdf)
        try:
            page = document[0].render(scale=300 / 72, grayscale=True).to_pil()
        finally:
            document.close()
        x, y = piece_labels.origin(0)
        box = [round(v * 300 / 72) for v in (x, 792 - y - piece_labels.LABEL_H, x + piece_labels.LABEL_W, 792 - y)]
        first = zbar_decode(page.crop(box))
        self.assertEqual(first, [labels[0]['url']])
        self.assertEqual(parse_piece_code(first[0]), labels[0]['code'])
        for index, label in enumerate(labels):          # every label on the sheet, scanned one at a time
            x, y = piece_labels.origin(index)
            box = [round(v * 300 / 72) for v in (x, 792 - y - piece_labels.LABEL_H, x + piece_labels.LABEL_W, 792 - y)]
            self.assertEqual([parse_piece_code(t) for t in zbar_decode(page.crop(box))], [label['code']], index)

    def test_qr_content_and_error_correction(self):
        import segno
        qr = segno.make(piece_url('WC-ABCD-EFGH'), error='m', micro=False, boost_error=False)
        self.assertEqual(qr.error, 'M')
        modules = len(qr.matrix) + 2 * piece_labels.QR_BORDER
        dark = piece_labels.QR_SIZE * len(qr.matrix) / modules
        self.assertGreaterEqual(dark / 72, 0.9)                  # the code itself is at least 0.9" square


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class LabelViewTests(LabelBase):
    def setUp(self):
        super().setUp()
        self.front = Printer.objects.create(name='Brother Front Counter', kind='letter', host='192.0.2.20', port=631)
        self.office = Printer.objects.create(name='Brother MFC Office', kind='letter', host='192.0.2.113', port=631,
                                             color=True, media_source='tray-1')
        self.tray2 = Printer.objects.create(name='Brother MFC Office \u2013 Tray 2', kind='letter', host='192.0.2.113',
                                            port=631, media_source='tray-2')
        self.tray3 = Printer.objects.create(name='Brother MFC Office \u2013 Tray 3', kind='letter', host='192.0.2.113',
                                            port=631, color=True, media_source='tray-3')

    def page(self):
        return self.client.get(f'/purchasing/po/{self.po.token}/')

    def test_po_page_has_the_label_controls_and_defaults_to_tray_one(self):
        response = self.page()
        self.assertEqual(response.context['label_selected_printer'], str(self.office.pk))
        self.assertEqual(response.context['selected_printer'], str(self.tray2.pk))     # POs keep Tray 2
        self.assertContains(response, 'id="label-print"')
        self.assertContains(response, f'action="/purchasing/po/{self.po.token}/labels/pdf/"')
        self.assertContains(response, 'href="/purchasing/labels/alignment/"')
        voucher = save_purchase(header(self.vendor), [dict(item=self.chair, quantity=Decimal('1'), cost=Decimal('1'))], 'voucher', 'hold')
        self.assertNotContains(self.client.get(f'/purchasing/voucher/{voucher.token}/'), 'id="label-print-bar"')

    def test_print_labels_sends_one_job_and_remembers_its_own_printer(self):
        with mock.patch('reports.direct_print.print_report', return_value='Sent 1 page to X at 1:00 PM.') as sent:
            response = self.client.post(f'/purchasing/po/{self.po.token}/labels/print/', {'printer': self.front.pk, 'copies': '2'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('16 labels', response.json()['message'])
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(sent.call_args.args[0], self.front)
        self.assertEqual(pages(sent.call_args.args[1]), 2)                     # 2 copies x 8 labels
        self.assertEqual(self.client.session['label_printer'], str(self.front.pk))
        self.assertNotIn('po_printer', self.client.session)
        page = self.page()
        self.assertEqual(page.context['label_selected_printer'], str(self.front.pk))
        self.assertEqual(page.context['selected_printer'], str(self.tray2.pk))   # PO printer untouched
        codes = list(PieceLabel.objects.values_list('code', flat=True))
        with mock.patch('reports.direct_print.print_report', return_value='Sent') as sent:
            self.client.post(f'/purchasing/po/{self.po.token}/labels/print/', {'printer': self.office.pk, 'copies': '1'})
        self.assertEqual(list(PieceLabel.objects.values_list('code', flat=True)), codes)   # reprint, same codes

    def test_named_printer_setting_wins(self):
        with override_settings(PIECE_LABEL_PRINTER='Brother MFC Office \u2013 Tray 3'):
            self.assertEqual(self.page().context['label_selected_printer'], str(self.tray3.pk))
        with override_settings(PIECE_LABEL_PRINTER='Not a printer'):
            self.assertEqual(self.page().context['label_selected_printer'], str(self.office.pk))

    def test_preview_pdf_and_alignment_sheet(self):
        with mock.patch('reports.direct_print.print_report') as sent:
            response = self.client.post(f'/purchasing/po/{self.po.token}/labels/pdf/', {'copies': '1', 'outlines': '1'})
            alignment = self.client.get('/purchasing/labels/alignment/')
        sent.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(pages(response.content), 1)
        self.assertEqual(PieceLabel.objects.count(), 8)
        self.assertEqual(alignment['Content-Type'], 'application/pdf')
        self.assertEqual(self.client.get(f'/purchasing/po/{self.po.token}/labels/pdf/').status_code, 405)   # POST only

    def test_refusals(self):
        missing = uuid.uuid4()
        with mock.patch('reports.direct_print.print_report') as sent:
            r = self.client.post(f'/purchasing/po/{missing}/labels/print/', {'printer': self.office.pk, 'copies': '1'})
            self.assertEqual((r.status_code, r.json()['error']), (404, 'Save the purchase order first.'))
            r = self.client.post(f'/purchasing/po/{missing}/labels/pdf/', {'copies': '1'})
            self.assertContains(r, 'Save the purchase order first.', status_code=404)
            empty = PurchaseDocument.objects.create(token=uuid.uuid4(), kind='po', status='open', vendor=self.vendor,
                                                    vendor_name='Sample Builder', total=0)
            r = self.client.post(f'/purchasing/po/{empty.token}/labels/print/', {'printer': self.office.pk, 'copies': '1'})
            self.assertEqual(r.status_code, 400)
            self.assertIn('no items with a quantity to label', r.json()['error'])
            r = self.client.post(f'/purchasing/po/{self.po.token}/labels/print/', {'printer': self.office.pk, 'copies': '6'})
            self.assertEqual(r.json()['error'], 'Copies must be 1 to 5.')
            r = self.client.post(f'/purchasing/po/{self.po.token}/labels/print/', {'printer': '', 'copies': '1'})
            self.assertEqual(r.json()['error'], 'Choose a printer.')
            save_purchase(header(self.vendor, self.po.token, self.po.version), [], 'po', 'cancel')
            r = self.client.post(f'/purchasing/po/{self.po.token}/labels/print/', {'printer': self.office.pk, 'copies': '1'})
            self.assertIn('was cancelled', r.json()['error'])
        sent.assert_not_called()
        self.assertEqual(PieceLabel.objects.count(), 0)
