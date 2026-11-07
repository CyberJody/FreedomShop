# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side receipt printing: render a 72mm raster image and send it to a Star printer on port 9100.

Proven on the TSP143IIILAN (futurePRNT, raster only) on September 26, 2026. No driver or CUPS is needed:
the printer accepts Star raster commands directly. Printed output is marked as testing.
"""
import socket
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from . import accounts
from .models import PAYMENT_METHODS, Printer, money, plain
from shop_settings.config import business

WIDTH = 576  # 72mm printable width at 203 dpi, confirmed edge to edge on the TSP143IIILAN
MARGIN = 8
TEST_NOTICE = 'TESTING - NOT A VALID RECEIPT'


def __getattr__(name):
    # Older module constants, now Business Settings: the receipt's return policy and header lines.
    if name == 'FOOTER':
        return business().return_policy
    if name == 'STORE_LINES':
        return business().receipt_lines()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def logo_path():
    """The shop's logo file: the one uploaded in Business Settings, else settings.PRINT_LOGO_FILE; None when neither."""
    shop = business()
    if shop.logo:
        try:
            path = Path(shop.logo.path)
        except (ValueError, NotImplementedError):
            path = None
        if path is not None and path.exists():
            return path
    path = getattr(settings, 'PRINT_LOGO_FILE', '')
    return Path(path) if path and Path(path).exists() else None


def print_heading(r, width=440, name_size=48, tagline_size=18):
    """The logo, or the business name (and tagline) in text when there is no logo. Returns the text heading, if any."""
    art = logo(width)
    if art:
        r.picture(art)
        return None
    shop = business()
    r.text(shop.name, name_size, 'center', bold=True)
    if shop.tagline:
        r.text(shop.tagline, tagline_size, 'center')
    return shop.name


def print_store_lines(r, heading=None):
    """The receipt header lines under the logo (first one bold), leaving out a first line that only repeats the heading."""
    lines = business().receipt_lines()
    if lines and heading and lines[0] == heading:
        lines = ['', *lines[1:]]   # the heading already shows it; the address lines follow as usual
    if lines and lines[0]:
        r.text(lines[0], 26, 'center', bold=True)
    for line in lines[1:]:
        r.text(line, 22, 'center')


def testing_marks():
    """Testing bars on printed documents, until the shop is marked live (Business.live, set at the end of setup).
    PRINT_TESTING_MARKS = False in the site settings is an override that turns them off whatever Business says."""
    return getattr(settings, 'PRINT_TESTING_MARKS', True) and not business().live


def logo(width):
    """The logo scaled to a pixel width, or None when there is no artwork file."""
    path = logo_path()
    if path is None:
        return None
    return _scaled_logo(str(path), path.stat().st_mtime_ns, width)


@lru_cache(maxsize=8)
def _scaled_logo(path, mtime_ns, width):  # keyed on the file and its age, so a new upload is picked up
    art = Image.open(path).convert('L')
    art = art.resize((width, round(art.height * width / art.width)), Image.LANCZOS)
    return art.point(lambda v: 0 if v < 128 else 255)


def document_tax_label(doc, gap):
    """The receipt / order tax line label: "Tax exempt:" on an exempt sale; with one rate, the document's tax location
    and the rate ("Main St 7 % Tax:") when it is that location's rate, else just the rate; "Tax:" for mixed rates."""
    from shop_settings.config import tax
    rates = {line.tax_rate for line in doc.lines.all() if line.tax_rate}
    if doc.tax_exempt:
        return 'Tax exempt:'
    if len(rates) != 1:
        return 'Tax:'
    rate = rates.pop()
    config = tax()
    location = config.location(config.section_of(doc.tax_location, False))
    if location.is_exempt:   # an "exempt" location name on a sale that is not tax-exempt: the default location charged it
        location = config.default_location
    if rate == location.rate:
        return f'{location.receipt_label}{gap}{quantity(rate)} % Tax:'
    return f'{quantity(rate)} % Tax:'


class PrintError(Exception):
    pass


def receipt_printers():
    return Printer.objects.filter(active=True, kind='receipt')


def font(size):
    return ImageFont.load_default(size=size)


class Receipt:
    """Collects drawing steps top to bottom, then paints them onto an image of the final height."""

    def __init__(self):
        self.y = MARGIN
        self.steps = []
        self.measure = ImageDraw.Draw(Image.new('1', (1, 1)))

    def width(self, text, size, bold=False):
        return self.measure.textlength(text, font=font(size)) + (2 if bold else 0)

    def text(self, text, size=22, align='left', bold=False, x=None, advance=True):
        w = self.width(text, size, bold)
        if x is None:
            x = {'left': MARGIN, 'center': (WIDTH - w) / 2, 'right': WIDTH - MARGIN - w}[align]
        elif align == 'right':
            x -= w
        self.steps.append(('text', x, self.y, text, size, bold))
        if advance:
            self.y += size + 6

    def wrap(self, text, size=22, align='left', bold=False, indent=0):
        for paragraph in (text or '').splitlines() or ['']:
            line = ''
            for word in paragraph.split():
                trial = f'{line} {word}'.strip()
                if line and self.width(trial, size, bold) > WIDTH - 2 * MARGIN - indent:
                    self.text(line, size, align, bold, x=MARGIN + indent if align == 'left' else None)
                    line = word
                else:
                    line = trial
            if line:
                self.text(line, size, align, bold, x=MARGIN + indent if align == 'left' else None)

    def columns(self, cells, size=22, bold=False):
        """cells: (text, x, align) with x as the left edge, or the right edge for right-aligned cells."""
        for text, x, align in cells:
            self.text(text, size, align, bold, x=x, advance=False)
        self.y += size + 6

    def rule(self, weight=2):
        self.y += 4
        self.steps.append(('rule', self.y, weight))
        self.y += weight + 6

    def banner(self, text, size=24):
        self.steps.append(('bar', self.y, size + 12))
        self.steps.append(('text-white', (WIDTH - self.width(text, size, True)) / 2, self.y + 5, text, size))
        self.y += size + 20

    def space(self, amount=12):
        self.y += amount

    def picture(self, img):
        self.steps.append(('image', (WIDTH - img.width) // 2, self.y, img))
        self.y += img.height

    def image(self):
        img = Image.new('1', (WIDTH, self.y + MARGIN), 1)
        draw = ImageDraw.Draw(img)
        for step in self.steps:
            if step[0] == 'text':
                _, x, y, text, size, bold = step
                draw.text((x, y), text, font=font(size), fill=0, stroke_width=1 if bold else 0, stroke_fill=0)
            elif step[0] == 'text-white':
                _, x, y, text, size = step
                draw.text((x, y), text, font=font(size), fill=1, stroke_width=1, stroke_fill=1)
            elif step[0] == 'rule':
                draw.rectangle([MARGIN, step[1], WIDTH - MARGIN, step[1] + step[2] - 1], fill=0)
            elif step[0] == 'tick':
                draw.line([step[1], step[2], step[1], step[2] + step[3]], fill=0)
            elif step[0] == 'bar':
                draw.rectangle([0, step[1], WIDTH - 1, step[1] + step[2]], fill=0)
            elif step[0] == 'image':
                img.paste(step[3].convert('1'), (step[1], step[2]))
        return img


def dollars(value):
    """$1,234.50; negatives (returns, refunds) as -$55.90, not $-55.90."""
    amount = money(value)
    return f'-${-amount:,.2f}' if amount < 0 else f'${amount:,.2f}'


def price_text(value):
    """Unit prices in whole cents, rounded half-up."""
    return dollars(value)


def quantity(value):
    return f'{value.normalize():f}' if value == value.to_integral() else f'{value:f}'.rstrip('0')


def bill_to(doc):
    """BILL TO block, the same on every receipt: name, street line(s), City ST zip, then every phone on file,
    one per line. A typed name with no customer record prints the name only. No email (not requested)."""
    c = doc.customer
    if not c:
        return doc.customer_name
    return '\n'.join(v for v in [(doc.customer_name or '').strip(), *c.address_lines(), *c.phone_numbers()] if v)


def render_receipt(doc, reprint=False):
    r = Receipt()
    if testing_marks():
        r.banner(TEST_NOTICE)
    when = timezone.localtime(doc.completed_at or doc.created_at)
    r.columns([(when.strftime('%-m/%-d/%Y %-I:%M %p'), MARGIN, 'left'),
               ('Held Receipt' if doc.status == 'held' else f'Sales Receipt #{doc.number}', WIDTH - MARGIN, 'right')], size=20)
    if getattr(doc, 'type_label', ''):
        r.text(doc.type_label.upper(), 22, 'center', bold=True)
    if doc.status == 'cancelled':
        r.text('CANCELLED', 26, 'center', bold=True)
    if doc.status == 'held':
        r.text('HELD', 26, 'center', bold=True)
    elif reprint:
        r.text('REPRINTED', 22, 'center', bold=True)
    r.space(6)
    heading = print_heading(r)
    r.space(8)
    print_store_lines(r, heading)
    r.space(14)
    customer_block = bill_to(doc)
    if customer_block:
        r.text('BILL TO:', 22, bold=True)
        r.wrap(customer_block, 22)
    if doc.customer_po:
        r.text(f'Customer PO: {doc.customer_po}', 20)
    r.space(10)

    qty_x, price_x, ext_x = 330, 452, WIDTH - MARGIN
    r.columns([('Item Name', MARGIN, 'left'), ('Qty', qty_x, 'right'), ('Price', price_x, 'right'), ('Ext.', ext_x, 'right')], 20, bold=True)
    r.rule(2)
    for line in doc.lines.all():
        # Item Name, not the QB item number.
        name = line.item_name or line.sku or 'MISC'
        qty_text = quantity(line.quantity)
        numbers = [(qty_text, qty_x, 'right'), (price_text(line.price), price_x, 'right'), (dollars(line.amount), ext_x, 'right')]
        if r.width(name, 20) <= qty_x - r.width(qty_text, 20) - 12 - MARGIN:
            r.columns([(name, MARGIN, 'left')] + numbers, 20)
        else:  # long names get their own line so they never run into the Qty column
            r.wrap(name, 20)
            r.columns(numbers, 20)
        r.wrap(line.description or line.item_name, 20, indent=0)
        # The line's discount as the sales order prints it: '10 % off $1,379.95 - Sale', '$35.00 off $484.95 -
        # Sale'; a return line shows the discount it was sold with.
        original = line.returned_from_line if getattr(line, 'returned_from_line_id', None) else None
        sold = original if original is not None else line
        if sold.discount_percent or sold.discounted or sold.discount_reason or sold.discount_note:
            from .letter_printing import line_discount_text
            r.wrap(line_discount_text(sold), 20)
        if original is not None:
            r.text(f'Return Item - Receipt# {original.document.number}', 20, bold=True)
            if line.return_reason_text:
                r.wrap(f'Reason: {line.return_reason_text}', 20)
            if line.damaged:
                r.text('Damaged - not restocked', 20)
    r.rule(2)

    label_x = 420
    r.columns([('Subtotal:', label_x, 'right'), (dollars(doc.subtotal), ext_x, 'right')], 22)
    if doc.discount:
        # Receipt-level discount, taken off the subtotal before tax: Subtotal - Discount + Tax = Total, as QB prints.
        r.columns([('Discount:', label_x, 'right'), ('- ' + dollars(doc.discount), ext_x, 'right')], 22)
    if doc.discount_reason or doc.discount_note:
        from .models import discount_text
        r.wrap('Receipt discount: ' + discount_text(doc.discount_reason, doc.discount_note), 20)
    tax_label = document_tax_label(doc, '   ')
    tax = doc.tax or Decimal('0')
    r.columns([(tax_label, label_x, 'right'), (dollars(tax) if tax < 0 else '+ ' + dollars(tax), ext_x, 'right')], 22)
    if getattr(doc, 'shipping', Decimal('0')):
        r.columns([('Shipping:', label_x, 'right'), (dollars(doc.shipping), ext_x, 'right')], 22)
    r.columns([('RECEIPT TOTAL:', label_x, 'right'), (dollars(doc.total) if doc.total is not None else 'not recorded', ext_x, 'right')], 24, bold=True)
    r.space(10)

    # the printed receipt never shows the salesperson's name — not the active PIN cashier, not an
    # imported QB cashier. The name stays on screen and on the document for sales-by-person attribution.
    methods = dict(PAYMENT_METHODS)
    if getattr(doc, 'imported', False):
        for tender in doc.tenders.all():
            label = tender.label + (f' #{tender.reference}' if tender.reference else '') + (' (memo)' if tender.use_in_total else '')
            r.columns([(label + ':', label_x, 'right'), (dollars(tender.amount) if tender.amount is not None else '—', ext_x, 'right')], 22)
    elif doc.source_order_id:
        r.text(f'Paid on sales order {doc.source_order.number}', 22)
        qb = doc.source_order.qb_deposit
        if qb:
            # Pickup of an order imported from QuickBooks POS: the deposit taken there, then app payments.
            r.columns([(f"{qb['label']}:", label_x, 'right'), (dollars(qb['amount']), ext_x, 'right')], 22)
        for payment in doc.source_order.payments.all():
            if not payment.is_tender:
                continue   # a sales tax adjustment is not money
            r.columns([(f'{payment.label}:', label_x, 'right'), (dollars(payment.amount), ext_x, 'right')], 22)
    elif doc.total is not None and doc.total < 0:
        # Refund (B1): what was given back and how, instead of Amount Tendered / Change.
        for entry in doc.payment_breakdown or []:
            label = entry.get('tender_name') or methods.get(entry['method'], entry['method'])
            r.columns([(f'Refund given ({label}):', label_x, 'right'), (dollars(-Decimal(entry['amount'])), ext_x, 'right')], 22)
    if not getattr(doc, 'imported', False) and not doc.source_order_id:
        if doc.total is not None and doc.total >= 0 and doc.tendered is not None:
            # (QB POS layout): each tender with the amount handed over; Change sits directly under the cash line.
            for line in doc.tender_lines:
                r.columns([(line['label'] + ':', label_x, 'right'), (dollars(line['amount']), ext_x, 'right')], 22)
        if accounts.shows_account(doc):
            # QB POS charge-account receipt: signature block when charged, then Previous / new Account Balance.
            if accounts.charged(doc) > 0:
                r.space(24)
                r.text('Signature ______________________', 22)
                r.wrap(accounts.SIGNATURE_TEXT, 18, 'center')
            previous, balance = accounts.balances(doc)
            r.space(10)
            r.columns([('Previous Account Balance:', label_x, 'right'), (dollars(previous), ext_x, 'right')], 22, bold=True)
            r.columns([('Account Balance:', label_x, 'right'), (dollars(balance), ext_x, 'right')], 22, bold=True)
    from .pickup import printed_line
    awaiting = printed_line(doc)
    if awaiting:
        # the goods are paid for and still in the store (both copies); reprints after pickup leave it off.
        r.space(10)
        r.text(awaiting, 24, 'center', bold=True)
    if doc.notes:
        r.space(10)
        r.wrap(doc.notes, 20)
    r.space(30)
    if business().return_policy:
        r.wrap(business().return_policy, 20, 'center', bold=True)
    r.space(24)
    r.text(doc.number, 26, 'center', bold=True)
    r.space(10)
    if testing_marks():
        r.banner(TEST_NOTICE)
    else:
        r.space(20)
    return r.image()


def render_sample_receipt():
    """Business Settings' test receipt: the header, a sample line and the footer exactly as a receipt lays them out,
    between testing bars. Rendered for the screen only; nothing is sent to a printer."""
    r = Receipt()
    r.banner('TEST RECEIPT - NOT A SALE')
    r.columns([(timezone.localtime().strftime('%-m/%-d/%Y %-I:%M %p'), MARGIN, 'left'), ('Sales Receipt #SAMPLE', WIDTH - MARGIN, 'right')], size=20)
    r.space(6)
    heading = print_heading(r)
    r.space(8)
    print_store_lines(r, heading)
    r.space(14)
    r.rule(2)
    r.columns([('Sample item', MARGIN, 'left'), ('$0.00', WIDTH - MARGIN, 'right')], 22)
    r.rule(2)
    r.space(30)
    if business().return_policy:
        r.wrap(business().return_policy, 20, 'center', bold=True)
    r.space(24)
    r.banner('TEST RECEIPT - NOT A SALE')
    return r.image()


def star_raster(img):
    """Star raster job. ESC FF NUL (execute form feed) is required: without it the TSP143 printed nothing."""
    esc = b'\x1b'
    bytes_per_line = WIDTH // 8
    out = bytearray(esc + b'@' + esc + b'*rR' + esc + b'*rA' + esc + b'*rP0\x00')
    # Pillow mode "1" packs 1 as white; Star wants 1 as a printed (black) dot.
    data = img.convert('1').tobytes()
    for row in range(img.height):
        line = bytes(b ^ 0xFF for b in data[row * bytes_per_line:(row + 1) * bytes_per_line])
        out += b'b' + bytes([bytes_per_line & 0xFF, bytes_per_line >> 8]) + line
    out += esc + b'\x0c\x00' + esc + b'*rB'
    return bytes(out)


def printer_status(reply):
    """Decode the Star automatic status reply; returns a warning or ''."""
    if len(reply) < 6 or reply[0] & 0x91 != 0x01:
        return ''
    if reply[2] & 0x20:
        return 'The printer cover is open.'
    if reply[5] & 0x08:
        return 'The printer is out of paper.'
    if reply[2] & 0x08:
        return 'The printer is offline.'
    return ''


def receipt_printer(printer_id):
    printer = receipt_printers().filter(pk=printer_id).first() if str(printer_id).isdigit() and len(str(printer_id)) <= 18 else None
    if not printer:
        raise PrintError('Choose a receipt printer.')
    return printer


def send(printer, job):
    try:
        with socket.create_connection((printer.host, printer.port), timeout=5) as conn:
            conn.sendall(job)
            if printer.kind == 'label':
                # The USB raw listener finishes its job at EOF; there is no
                # Star status reply to wait for or decode on a Zebra.
                return printer.name
            conn.settimeout(3)
            try:
                reply = conn.recv(64)
            except socket.timeout:
                reply = b''
    except OSError:
        raise PrintError(f"Could not reach {printer.name}. Check that it is on and connected.")
    warning = printer_status(reply) if printer.kind != 'label' else ''
    if warning:
        raise PrintError(f"{printer.name}: {warning} The page may print once this is fixed, so check before printing again.")
    return printer.name


def check_status(printer):
    """Check without printing. Star printers answer on 9100 with their status; Letter printers answer over IPP."""
    if printer.kind == 'letter':
        from .letter_printing import letter_status
        return letter_status(printer)
    try:
        with socket.create_connection((printer.host, printer.port), timeout=3) as conn:
            conn.settimeout(1.5)
            try:
                reply = conn.recv(64)
            except socket.timeout:
                reply = b''
    except OSError:
        return {'ok': False, 'status': 'Not reachable'}
    warning = printer_status(reply) if printer.kind != 'label' else ''
    if warning:
        return {'ok': False, 'status': warning.rstrip('.')}
    return {'ok': True, 'status': 'Ready' if reply else 'Online'}


def render_test_page(printer):
    r = Receipt()
    r.banner('PRINTER TEST - NOT A RECEIPT')
    r.space(10)
    r.text(business().name, 44, 'center', bold=True)
    r.space(6)
    r.text(printer.name, 30, 'center', bold=True)
    for line in [printer.model, printer.location, f'{printer.host}:{printer.port}', printer.mac_address,
                 timezone.localtime().strftime('%-m/%-d/%Y %-I:%M:%S %p'), 'Sent from the app server']:
        if line:
            r.text(line, 22, 'center')
    r.space(16)
    r.steps.append(('rule', r.y, 2))
    for x in range(0, WIDTH, 8):  # 1 mm ticks; long tick every 10 mm
        r.steps.append(('tick', x, r.y, 24 if x % 80 == 0 else 10))
    r.y += 30
    r.columns([('0', MARGIN, 'left'), ('edge-to-edge ruler', None, 'center'), (str(WIDTH), WIDTH - MARGIN, 'right')], 18)
    r.space(10)
    r.banner('PRINTER TEST - NOT A RECEIPT')
    return r.image()


def print_test_page(printer):
    if not printer.active:
        raise PrintError('This printer is inactive. Edit it and check Active before printing.')
    if printer.kind == 'label':
        from .label_printing import test_zpl
        return send(printer, test_zpl())
    if printer.kind == 'letter':
        from .letter_printing import render_letter_test_page, send_letter
        return send_letter(printer, render_letter_test_page(printer), f'Printer test - {printer.name}')
    return send(printer, star_raster(render_test_page(printer)))


def receipt_copies(doc):
    """(matching QB): a completed receipt (sale, return, pickup, payment on account) prints 2 identical copies on
    the receipt printer, on Save & Print and on Reprint; a held or cancelled receipt prints 1. Letter paper and Create
    PDF stay at 1 copy."""
    return business().receipt_copies if getattr(doc, 'status', '') == 'completed' else 1


def receipt_job(doc, img):
    """One Star job holding the receipt `receipt_copies(doc)` times: each copy is a complete raster job of its own
    (ESC @... ESC FF NUL ESC *rB), byte for byte the single-receipt job that prints and cuts today, sent back to back
    over one connection, so every copy is cut and nothing else can print between them."""
    return star_raster(img) * receipt_copies(doc)


def print_receipt(doc, printer_id, reprint=True):
    return send(receipt_printer(printer_id), receipt_job(doc, render_receipt(doc, reprint=reprint)))
