# SPDX-License-Identifier: AGPL-3.0-or-later
"""No real network traffic: all label sends use a fake socket, all IPP requests are mocked."""
import re
import socket
import struct
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from sales import label_printing as lp
from sales.label_printing import TagText, prefill, render_tag, tag_fields, tag_zpl, text_zpl, to_label
from sales.letter_printing import tag_pdf
from sales.models import Item, Printer
from sales.printing import PrintError, check_status, send


def decode_gfa(job):
    """Independent decoder for the ^GFA field (Zebra ASCII compression) -> (bytes_per_row, raw bytes)."""
    match = re.search(rb'\^GFA,(\d+),(\d+),(\d+),([^\^]*)\^FS', job)
    assert match, 'no ^GFA field'
    total, total2, per_row = int(match[1]), int(match[2]), int(match[3])
    assert total == total2
    data, width = match[4].decode('ascii'), per_row * 2
    rows, row, count, previous = [], '', 0, ''
    for ch in data:
        if 'G' <= ch <= 'Y':
            count += ord(ch) - ord('G') + 1
        elif 'g' <= ch <= 'z':
            count += (ord(ch) - ord('g') + 1) * 20
        elif ch in ',!':
            row += ('0' if ch == ',' else 'F') * (width - len(row))
        elif ch == ':':
            assert row == ''
            row = previous
        else:
            assert ch in '0123456789ABCDEF', ch
            row += ch * (count or 1)
            count = 0
        if len(row) == width:
            rows.append(row)
            previous, row = row, ''
        assert len(row) < width
    raw = bytes.fromhex(''.join(rows))
    assert len(raw) == total, (len(raw), total)
    return per_row, raw


def ink_bbox(image, box=None):
    """Bounding box of black pixels in a mode-'1' image (optionally inside box, in image coordinates)."""
    part = image.crop(box) if box else image
    bbox = Image.eval(part.convert('L'), lambda v: 255 - v).getbbox()
    if bbox and box:
        bbox = (bbox[0] + box[0], bbox[1] + box[1], bbox[2] + box[0], bbox[3] + box[1])
    return bbox


def sent_bits(label):
    padded = Image.new('1', (816, 599), 1)
    padded.paste(label, (0, 0))
    return bytes(b ^ 0xFF for b in padded.tobytes())


SAMPLE = TagText(name='"LINCOLN" SIDE CHAIR', code='MWC-NEW"LINCOLN"-DM', wood='BARNWOOD', size='DINING', price='$439.95')


class TagRendererTests(SimpleTestCase):
    def test_portrait_tag_turned_onto_the_label(self):
        tag = render_tag(SAMPLE)
        self.assertEqual((tag.mode, tag.size), ('1', (599, 812)))
        label = to_label(tag)
        self.assertEqual(label.size, (812, 599))
        # The logo is at the top of the portrait tag; turned clockwise it lies along the label's right edge.
        self.assertIsNotNone(ink_bbox(tag, (0, 0, 599, 40)))
        self.assertIsNotNone(ink_bbox(label, (772, 0, 812, 599)))
        self.assertEqual(list(label.getdata()), list(tag.transpose(Image.Transpose.ROTATE_270).getdata()))
        with mock.patch.object(lp, 'TAG_ROTATION', 'ccw'):
            self.assertEqual(list(to_label(tag).getdata()), list(tag.transpose(Image.Transpose.ROTATE_90).getdata()))

    def test_gfa_payload_round_trips_and_job_is_small(self):
        job = text_zpl(SAMPLE, 3)
        for part in [b'^XA', b'^PW812', b'^LL599', b'^LH0,0', b'^FO0,0^GFA,', b'^PQ3', b'^XZ']:
            self.assertIn(part, job)
        self.assertEqual(job.count(b'^XA'), 1)
        self.assertNotIn(b'^BC', job)        # no barcode
        per_row, raw = decode_gfa(job)
        self.assertEqual(per_row, 102)
        self.assertEqual(len(raw), 102 * 599)
        self.assertEqual(raw, sent_bits(to_label(render_tag(SAMPLE))))
        self.assertLess(len(job), 40000)      # compressed; plain hex would be ~122 kB
        job.decode('ascii')

    def test_copies_validated(self):
        for copies in [0, 11, '3', True]:
            with self.assertRaises(ValueError):
                text_zpl(SAMPLE, copies)

    def test_cents_are_small_and_raised(self):
        self.assertEqual(lp.split_price('$1374.95'), ('$1374', '95'))
        self.assertEqual(lp.split_price('1,374'), ('$1,374', '00'))
        self.assertEqual(lp.split_price('$12.5'), ('$12', '50'))
        self.assertEqual(lp.split_price('CALL'), ('CALL', ''))
        tag = render_tag(SAMPLE)
        price = ink_bbox(tag, (0, lp.PRICE_BASELINE - 160, 599, lp.PRICE_BASELINE + 2))
        cents = ink_bbox(tag, (price[2] - 40, price[1], price[2], price[3]))
        self.assertLess(cents[3], lp.PRICE_BASELINE - 50)        # cents end well above the baseline
        self.assertLessEqual(abs(cents[1] - price[1]), 4)        # tops level with the dollar digits
        self.assertGreaterEqual(price[3], lp.PRICE_BASELINE - 2)  # dollars sit on the fixed baseline

    def test_price_and_logo_do_not_move_with_long_text(self):
        short = render_tag(SAMPLE)
        long = render_tag(TagText(name='W' * 120, code='X' * 80, wood='VERY LONG WOOD NAME ' * 4, size='12 X 34 X 56 ' * 6,
                                  price='$439.95'))
        for box in [(0, 0, 599, 185), (0, lp.FINISHED_TOP, 599, 812)]:
            self.assertEqual(short.crop(box).tobytes(), long.crop(box).tobytes(), box)

    def test_seal_centers_between_the_text_above_and_below(self):
        half = lp.SEAL_DIAMETER // 2
        cases = [SAMPLE, TagText(name='TABLE', price='$99.00'),
                 TagText(name='LIVE-EDGE COFFEE TABLE WITH A LONG NAME', code='NR/COFFEETOP-REG PMW 2100-30', wood='OAK',
                         price='$729.95'),
                 TagText(name='BENCH', wood='RUSTIC HICKORY WITH A VERY LONG FINISH NAME', size='~23 X 42 X 19 IN TALL',
                         price='$129.95')]
        centers = set()
        for text in cases:
            with mock.patch.object(lp, 'draw_seal', wraps=lp.draw_seal) as seal:
                tag = render_tag(text)
            cx, cy = seal.call_args.args[1]
            ring = ink_bbox(tag, (cx - half - 3, cy - 2, cx + half + 3, cy + 2))
            self.assertAlmostEqual((ring[0] + ring[2]) / 2, cx, delta=2)
            above = ink_bbox(tag, (0, lp.NAME_TOP, 599, cy - half - 3))
            below = ink_bbox(tag, (0, cy + half + 4, 599, lp.FINISHED_TOP))
            gap_above = cy - half - (above[3] if above else lp.NAME_TOP)
            gap_below = (below[1] if below else lp.FINISHED_TOP) - (cy + half)
            self.assertGreaterEqual(min(gap_above, gap_below), lp.SEAL_GAP - 1, text)
            self.assertLessEqual(abs(gap_above - gap_below), 3, (text, gap_above, gap_below))
            centers.add(cy)
        self.assertGreater(len(centers), 1)   # it really moves with the text

    def test_long_names_wrap_or_shrink_inside_their_boxes(self):
        text = TagText(name='LIVE-EDGE COFFEE TABLE W/ STEEL BASE AND A VERY LONG EXTRA DESCRIPTION OF THE FINISH',
                       code='NR/COFFEETOP-REG & PMW 2100-30 AND MORE CODES HERE', price='$729.95')
        name_size, name_lines, code_size, code_lines = lp.name_block(text)
        self.assertLessEqual(len(name_lines), 2)
        self.assertLessEqual(len(code_lines), 2)
        height = len(name_lines) * lp.line_height(name_size) + len(code_lines) * lp.line_height(code_size)
        self.assertLessEqual(lp.NAME_TOP + height, lp.SEAL_CENTER[1] - lp.SEAL_DIAMETER // 2)
        for line in name_lines:
            self.assertLessEqual(lp.font(lp.WIDE, name_size).getlength(line), lp.TEXT_W)
        _, lines, _, _ = lp.name_block(TagText(name='W' * 120))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith('…'))
        size, lines = lp.wood_block(TagText(wood='OAK ' * 20, size='~23 X 42 X 19"H'))
        self.assertLessEqual(len(lines), 4)
        self.assertLessEqual(len(lines) * lp.line_height(size),
                             lp.WOOD_BOTTOM - lp.SEAL_CENTER[1] - lp.SEAL_DIAMETER // 2)
        dollars, cents, size, _ = lp.price_layout('$99999.99')
        self.assertLess(size, lp.PRICE_SIZE)                      # a wide price shrinks instead of overflowing

    def test_seal_size_comes_from_the_constant(self):
        self.assertEqual(lp.SEAL_DIAMETER, round(lp.SEAL_STICKER_IN * 203 * lp.SEAL_SCALE))
        cx, cy = lp.SEAL_CENTER
        for diameter in [lp.SEAL_DIAMETER, 200]:
            with mock.patch.object(lp, 'SEAL_DIAMETER', diameter):
                tag = render_tag(TagText())
            ring = ink_bbox(tag, (0, cy - 2, 599, cy + 2))
            self.assertAlmostEqual(ring[2] - ring[0], diameter, delta=4)
            self.assertAlmostEqual((ring[0] + ring[2]) / 2, cx, delta=2)

    def test_prefill_matches_the_photographed_tags(self):
        bar = Item(sku='11530', name='BRF-B-52-H-N', description='RUSTIC COFFEE BAR. NATURAL HICKORY', price=Decimal('1374.95'))
        self.assertEqual(prefill(bar), TagText(name='RUSTIC COFFEE BAR', code='BRF-B-52-H-N', wood='NATURAL HICKORY',
                                               size='', price='$1374.95'))
        chair = Item(sku='2826', name='BW/RHICK78C-SY/EARTH', wood='RHICK', price=Decimal('384.95'),
                     description='RUSTIC HICKORY LINCOLN SIDE CHAIR\r\n2 TONE  SEELY SEAT / EARTHTONE FRAME')
        self.assertEqual(prefill(chair).name, 'LINCOLN SIDE CHAIR')
        self.assertEqual(prefill(chair).wood, 'RUSTIC HICKORY')
        lincoln = Item(sku='16090', name='MWC-NEW"LINCOLN"-DM', wood='BARNWOOD', price=Decimal('439.95'),
                       description='NEW CHAIR BUILT LIKE THE LINCOLN. RECLAIMED BARNWOOD.')
        self.assertEqual(prefill(lincoln).wood, 'BARNWOOD')            # the item's own Wood field wins
        self.assertEqual(prefill(lincoln).code, 'MWC-NEW"LINCOLN"-DM')
        top = Item(sku='5084', name='NR/COFFEETOP-REG', price=Decimal('379.95'),
                   description='WALNUT COFFEE TOP ONLY    RECTANGLE\n***TOP ONLY***\nLIVE EDGE  APPROX 23 X 42')
        self.assertEqual(prefill(top).size, '~23 X 42')
        self.assertEqual(prefill(top).wood, 'WALNUT')
        self.assertEqual(prefill(top).name, 'COFFEE TOP ONLY')
        self.assertEqual(lp.parse_size('LARGE BAR, APPROX 48W X 20D X 65H'), '~48W X 20D X 65H')
        self.assertEqual(lp.parse_wood('BARREL WOOD MICHAELS CHERRY'), '')   # a finish, not the wood
        plain = Item(sku='1', name='Gift Basket', size='Large', wood='174.95', description='', price=None)
        self.assertEqual(prefill(plain), TagText(name='Gift Basket', code='1', wood='', size='Large', price=''))

    def test_from_data_refuses_bad_input(self):
        self.assertEqual(lp.from_data({'name': '  A   B ', 'price': '$1'}).name, 'A B')
        with self.assertRaisesMessage(ValueError, 'Name is too long'):
            lp.from_data({'name': 'x' * 121})
        with self.assertRaisesMessage(ValueError, 'cannot be printed'):
            lp.from_data({'code': 'A\x00B'})
        fallback = TagText(name='From item', price='$5.00')
        self.assertEqual(lp.from_data({'name': 'Edited'}, fallback), TagText(name='Edited', price='$5.00'))

    def test_letter_pdf_has_one_tag_per_sheet_and_all_copies(self):
        import pypdfium2 as pdfium
        item = Item(name='Oak Cabinet', sku='15063', price='1234.56', description='Solid oak cabinet', upc='0012345')
        pdf = pdfium.PdfDocument(tag_pdf(item, 3))
        try:
            self.assertEqual(len(pdf), 3)
            for page in pdf:
                self.assertEqual(page.get_size(), (612, 792))
                text = page.get_textpage().get_text_range()
                self.assertIn('$1,234.56', text)
                self.assertIn('Item No. 15063', text)
        finally:
            pdf.close()
        self.assertEqual(tag_fields(item)[0][3], ['Demo Home & Hardware'])

    @mock.patch('sales.printing.socket.create_connection')
    def test_raw_send_and_status_do_not_decode_star_bytes(self, connect):
        printer = Printer(name='TEST Zebra', kind='label', host='127.0.0.1', port=9100)
        conn = connect.return_value.__enter__.return_value
        conn.recv.return_value = b'\x01\x00\x20\x00\x00\x08'
        job = tag_zpl(Item(name='BRF-B-52-H-N', sku='1', description='RUSTIC COFFEE BAR', price=Decimal('1374.95')))
        self.assertEqual(send(printer, job), printer.name)
        conn.sendall.assert_called_once_with(job)
        self.assertTrue(check_status(printer)['ok'])
        conn.recv.return_value = b''
        self.assertEqual(check_status(printer), {'ok': True, 'status': 'Online'})
        conn.recv.side_effect = socket.timeout
        self.assertEqual(check_status(printer), {'ok': True, 'status': 'Online'})
        connect.side_effect = OSError
        self.assertFalse(check_status(printer)['ok'])
        with self.assertRaises(PrintError):
            send(printer, job)


class LabelPrinterScreenTests(TestCase):
    def setUp(self):
        self.printer = Printer.objects.create(name='TEST Zebra', kind='label', host='127.0.0.1', port=9100)

    @mock.patch('sales.printing.socket.create_connection')
    def test_test_button_sends_the_new_bitmap_tag(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.recv.return_value = b''
        text = TagText(name='PRINTER TEST TAG', code='10/19/2026', price='$123.45')
        with mock.patch.object(lp, 'test_tag_text', return_value=text):
            response = self.client.post(f'/printers/{self.printer.pk}/test/')
        self.assertEqual(response.status_code, 200)
        job = conn.sendall.call_args.args[0]
        self.assertEqual(job, text_zpl(text))
        for part in [b'^PW812', b'^LL599', b'^GFA,']:
            self.assertIn(part, job)
        self.printer.active = False
        self.printer.save()
        connect.reset_mock()
        self.assertEqual(self.client.post(f'/printers/{self.printer.pk}/test/').status_code, 502)
        connect.assert_not_called()

    def test_add_edit_and_list_label_kind(self):
        response = self.client.post('/printers/new/', dict(name='TEST USB Zebra', kind='label',
            host='127.0.0.1', port=9100, active='on'))
        self.assertEqual(response.status_code, 302)
        printer = Printer.objects.get(name='TEST USB Zebra')
        response = self.client.post(f'/printers/{printer.pk}/', dict(name=printer.name, kind='label',
            host='127.0.0.1', port=9100, active='on', version=printer.version, location='TEST'))
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get('/printers/'), 'Label (Zebra 4×3 tags)')

    def test_color_and_tray_are_editable(self):
        page = self.client.get('/printers/new/')
        self.assertContains(page, 'name="color"')
        self.assertContains(page, '<option value="tray-1">Tray 1</option>', html=True)
        response = self.client.post('/printers/new/', dict(name='TEST Color Brother', kind='letter', host='192.0.2.10', port=631,
                                                            active='on', color='on', media_source='tray-1'))
        self.assertEqual(response.status_code, 302)
        printer = Printer.objects.get(name='TEST Color Brother')
        self.assertTrue(printer.color)
        self.assertEqual(printer.media_source, 'tray-1')
        self.assertEqual(self.client.post('/printers/new/', dict(name='TEST Bad Tray', kind='letter', host='192.0.2.11', port=631,
                                                                 active='on', media_source='tray-9')).status_code, 400)
        self.assertFalse(Printer.objects.create(name='TEST Default', kind='letter', host='192.0.2.12').color)


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class ItemTagScreenTests(TestCase):
    def setUp(self):
        self.item = Item.objects.create(sku='11530', name='BRF-B-52-H-N', description='RUSTIC COFFEE BAR. NATURAL HICKORY',
                                        price=Decimal('1374.95'), print_tags=True)
        self.zebra = Printer.objects.create(name='TEST Zebra', kind='label', host='127.0.0.1', port=9100)
        self.letter = Printer.objects.create(name='TEST Brother', kind='letter', host='192.0.2.1', port=631)

    def test_tag_generator_lists_item_tag_and_prefills(self):
        self.assertContains(self.client.get('/tags/'), 'href="/tags/item-tag/"')
        page = self.client.get(f'/tags/item-tag/?item={self.item.pk}')
        self.assertContains(page, 'value="RUSTIC COFFEE BAR"')
        self.assertContains(page, 'value="BRF-B-52-H-N"')
        self.assertContains(page, 'value="NATURAL HICKORY"')
        self.assertContains(page, 'value="$1374.95"')
        self.assertContains(page, '/tags/item-tag/preview.png?name=RUSTIC+COFFEE+BAR')
        self.assertContains(page, 'TEST Zebra')
        self.assertNotContains(page, 'TEST Brother')     # Zebra only on the Item Tag
        self.assertContains(self.client.get('/tags/item-tag/?item=999999'), 'not in the Item List')
        self.assertEqual(self.client.get('/tags/item-tag/').status_code, 200)

    def test_preview_png_and_bad_input(self):
        response = self.client.get('/tags/item-tag/preview.png', {'name': 'RUSTIC COFFEE BAR', 'price': '$1374.95'})
        self.assertEqual(response['Content-Type'], 'image/png')
        image = Image.open(BytesIO(response.content))
        self.assertEqual(image.size, (599, 812))
        self.assertEqual(list(image.convert('1').getdata()),
                         list(render_tag(TagText(name='RUSTIC COFFEE BAR', price='$1374.95')).getdata()))
        sent = Image.open(BytesIO(self.client.get('/tags/item-tag/preview.png', {'name': 'X', 'view': 'label'}).content))
        self.assertEqual(sent.size, (812, 599))
        self.assertEqual(self.client.get('/tags/item-tag/preview.png', {'name': 'x' * 500}).status_code, 400)
        self.assertEqual(self.client.get('/tags/item-tag/preview.png', {'code': 'a\x07b'}).status_code, 400)
        self.assertEqual(self.client.post('/tags/item-tag/preview.png').status_code, 405)

    @mock.patch('sales.printing.socket.create_connection')
    def test_print_sends_exactly_the_edited_preview(self, connect):
        conn = connect.return_value.__enter__.return_value
        edited = dict(name='"LINCOLN" SIDE CHAIR', code='MWC-NEW"LINCOLN"-DM', wood='BARNWOOD', size='DINING', price='$439.95')
        response = self.client.post('/tags/item-tag/print/', dict(edited, printer=self.zebra.pk, copies='2', item=self.item.pk))
        self.assertEqual(response.json(), {'message': 'Sent 2 tags to TEST Zebra.'})
        job = conn.sendall.call_args.args[0]
        self.assertEqual(job, text_zpl(TagText(**edited), 2))
        _, raw = decode_gfa(job)
        preview = Image.open(BytesIO(self.client.get('/tags/item-tag/preview.png', dict(edited, view='label')).content)).convert('1')
        self.assertEqual(raw, sent_bits(preview))
        self.assertEqual(self.client.session['label_printer'], str(self.zebra.pk))
        self.item.refresh_from_db()
        self.assertEqual(self.item.description, 'RUSTIC COFFEE BAR. NATURAL HICKORY')   # edits don't change the item
        self.assertContains(self.client.get('/tags/item-tag/'), f'value="{self.zebra.pk}" selected')

    @mock.patch('sales.printing.socket.create_connection')
    def test_print_refuses_bad_requests_without_sending(self, connect):
        url = '/tags/item-tag/print/'
        for data in [dict(printer=self.letter.pk, name='A'), dict(printer='x', name='A'), dict(printer=self.zebra.pk, name='A', copies='11'),
                     dict(printer=self.zebra.pk, name='x' * 200), dict(printer=self.zebra.pk),
                     dict(printer=self.zebra.pk, name='A', item='999999')]:
            self.assertEqual(self.client.post(url, data).status_code, 400, data)
        self.item.print_tags = False
        self.item.save()
        self.assertEqual(self.client.post(url, dict(printer=self.zebra.pk, name='A', item=self.item.pk)).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        connect.assert_not_called()

    @mock.patch('sales.printing.socket.create_connection')
    def test_item_price_tag_page_uses_the_editor(self, connect):
        page = self.client.get(f'/purchasing/items/{self.item.pk}/tag/')
        self.assertContains(page, 'id="zebra-tag"')
        self.assertContains(page, 'value="RUSTIC COFFEE BAR"')
        self.assertContains(page, 'Print Letter Tag')
        letter_select = page.content.split(b'id="tag-printer"')[1].split(b'</select>')[0]
        self.assertIn(b'TEST Brother', letter_select)
        self.assertNotIn(b'TEST Zebra', letter_select)
        response = self.client.post(f'/purchasing/items/{self.item.pk}/tag/print/',
                                    dict(printer=self.zebra.pk, copies='1', name='Edited name', code='BRF-B-52-H-N', price='$1374.95'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(connect.return_value.__enter__.return_value.sendall.call_args.args[0],
                         text_zpl(TagText(name='Edited name', code='BRF-B-52-H-N', wood='NATURAL HICKORY', price='$1374.95')))
        # fields not posted (wood here) fall back to the Item List suggestion


def decode_pwg_pages(data):
    """Decoder for gray or color PWG raster: list of (header, rows)."""
    assert data[:4] == b'RaS2'
    i, pages = 4, []
    while i < len(data):
        header = data[i:i + 1796]
        i += 1796
        width, height = struct.unpack('>II', header[372:380])
        size = struct.unpack('>I', header[388:392])[0] // 8
        rows = []
        while len(rows) < height:
            repeat = data[i]
            i += 1
            row = bytearray()
            while len(row) < width * size:
                c = data[i]
                i += 1
                if c == 128:
                    row += b'\xff' * (width * size - len(row))
                elif c < 128:
                    row += bytes(data[i:i + size]) * (c + 1)
                    i += size
                else:
                    count = (257 - c) * size
                    row += data[i:i + count]
                    i += count
            rows += [bytes(row)] * (repeat + 1)
        pages.append((header, rows))
    return pages


def ipp_attrs(blob):
    """name -> [(tag, raw value)] from an IPP attribute group blob; collection members fold into their attribute."""
    out, i, name = {}, 0, ''
    while i < len(blob):
        tag = blob[i]
        n = struct.unpack('>H', blob[i + 1:i + 3])[0]
        key = blob[i + 3:i + 3 + n].decode()
        i += 3 + n
        m = struct.unpack('>H', blob[i:i + 2])[0]
        value = blob[i + 2:i + 2 + m]
        i += 2 + m
        name = key or name
        out.setdefault(name, []).append((tag, value))
    return out


class ColorLetterPrinterTests(SimpleTestCase):
    def pages(self, color):
        from sales.letter_printing import PAGE_H, PAGE_W
        page = Image.new('RGB' if color else 'L', (PAGE_W, PAGE_H), 'white')
        page.paste((27, 107, 58) if color else 0, (300, 300, 900, 400))
        return [page]

    def send(self, printer, pages):
        from sales import letter_printing
        with mock.patch.object(letter_printing, 'ipp_request', return_value=(0, {})) as request:
            letter_printing.send_letter(printer, pages, 'Furniture Set Tag TEST')
        return request

    def test_color_printer_sends_color_raster_from_tray_1(self):
        printer = Printer(name='Color Brother', kind='letter', host='192.0.2.5', port=631, color=True, media_source='tray-1')
        request = self.send(printer, self.pages(True))
        print_job = request.call_args_list[-1]
        self.assertEqual(print_job.args[1], 0x0002)
        attrs = ipp_attrs(print_job.args[3])
        self.assertEqual(attrs['print-color-mode'], [(0x44, b'color')])
        self.assertNotIn('media', attrs)
        members = [v for t, v in attrs['media-col'] if t == 0x4A]
        self.assertEqual(members, [b'media-size', b'x-dimension', b'y-dimension', b'media-source'])
        self.assertIn((0x44, b'tray-1'), attrs['media-col'])
        self.assertEqual([t for t, v in attrs['media-col']].count(0x37), 2)   # nested collection closed
        (header, rows), = decode_pwg_pages(print_job.args[4])
        self.assertEqual(struct.unpack('>I', header[400:404])[0], 19)      # srgb
        self.assertEqual(struct.unpack('>I', header[388:392])[0], 24)
        self.assertEqual(rows[350][300 * 3:301 * 3], bytes((27, 107, 58)))  # the green survives
        self.assertEqual(rows[10][:3], b'\xff\xff\xff')

    def test_gray_pages_stay_monochrome_even_on_a_color_printer(self):
        for printer in [Printer(name='Brother', kind='letter', host='192.0.2.6'),
                        Printer(name='Color Brother', kind='letter', host='192.0.2.5', color=True)]:
            request = self.send(printer, self.pages(False))
            self.assertEqual(request.call_count, 1)          # no tray chosen: no printer query
            attrs = ipp_attrs(request.call_args.args[3])
            self.assertEqual(attrs['print-color-mode'], [(0x44, b'monochrome')])
            self.assertEqual(attrs['media'], [(0x44, b'na_letter_8.5x11in')])
            (header, rows), = decode_pwg_pages(request.call_args.args[4])
            self.assertEqual(struct.unpack('>I', header[400:404])[0], 18)  # sgray
        mono = Printer(name='Brother', kind='letter', host='192.0.2.6')
        attrs = ipp_attrs(self.send(mono, self.pages(True)).call_args.args[3])
        self.assertEqual(attrs['print-color-mode'], [(0x44, b'monochrome')])   # color pages on a mono printer

    def test_tray_keyword_falls_back_to_the_printers_own_name(self):
        from sales import letter_printing
        printer = Printer(name='Color Brother', kind='letter', host='192.0.2.5', media_source='tray-1')
        replies = [(0, {'media-source-supported': ['auto', 'main', 'manual']}), (0, {})]
        with mock.patch.object(letter_printing, 'ipp_request', side_effect=replies) as request:
            letter_printing.send_letter(printer, self.pages(False), 'TEST')
        self.assertEqual(request.call_args_list[0].args[1], 0x000B)
        self.assertIn((0x44, b'main'), ipp_attrs(request.call_args_list[1].args[3])['media-col'])
        with mock.patch.object(letter_printing, 'ipp_request', side_effect=[PrintError('x'), (0, {})]) as request:
            letter_printing.send_letter(printer, self.pages(False), 'TEST')
        self.assertIn((0x44, b'tray-1'), ipp_attrs(request.call_args_list[1].args[3])['media-col'])

    def test_green_pdf_pages_stay_color_only_for_color_printers(self):
        from reportlab.lib.colors import HexColor
        from reportlab.pdfgen import canvas
        from reports.direct_print import pages_from_pdf
        from sales.letter_printing import has_color
        out = BytesIO()
        pdf = canvas.Canvas(out, pagesize=(612, 792))
        pdf.setFillColor(HexColor('#1b6b3a'))
        pdf.drawString(100, 700, 'Reserve yours today')
        pdf.rect(100, 600, 200, 50, fill=1)
        pdf.showPage()
        pdf.setFillColor(HexColor('#000000'))
        pdf.drawString(100, 700, 'Black only')
        pdf.save()
        color_pages = pages_from_pdf(out.getvalue(), color=True)
        self.assertEqual([p.mode for p in color_pages], ['RGB', 'L'])
        self.assertTrue(has_color(color_pages[0]))
        self.assertEqual([p.mode for p in pages_from_pdf(out.getvalue())], ['L', 'L'])
