# SPDX-License-Identifier: AGPL-3.0-or-later
"""Letter sales-order printing: render 300 dpi pages and send them to a Brother printer as PWG raster over IPP.

The HL-L6200DW advertises image/pwg-raster (sgray_8, 300/600 dpi) on http://<host>:631/ipp/print, so no driver
or CUPS is needed. Layout follows the QB POS sales order sample (#5367). Printed output is marked as testing.
"""
import struct
import urllib.error
import urllib.request
from decimal import Decimal
from itertools import groupby

from django.utils import timezone
from PIL import Image, ImageDraw

from .printing import PrintError, document_tax_label, dollars, font, logo, quantity, testing_marks
from shop_settings.config import business

DPI = 300
PAGE_W, PAGE_H = 2550, 3300  # US Letter at 300 dpi
LEFT, RIGHT = 150, PAGE_W - 150  # half-inch margins; the printer's hardware margin is about 0.17 inch
TOP, BOTTOM = 150, PAGE_H - 150
BODY = 40  # about 9.6 pt
TEST_NOTICE = 'TESTING - NOT A VALID SALES ORDER'


def __getattr__(name):
    # Older module constants, now Business Settings: the sales order's policy, address block and store code.
    if name == 'FOOTER':
        return business().order_policy
    if name == 'STORE_BLOCK':
        return business().order_lines()
    if name == 'STORE_CODE':
        return business().short_code
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

# Sales order line columns: (label, x, align); x is the left edge, or the right edge when right-aligned.
# No Sold/Due columns: picked-up quantities stay on screen; the space goes to Item Description.
COLUMNS = [('LOC', LEFT, 'left'), ('Item Name', 330, 'left'), ('Item Description', 860, 'left'), ('Qty', 1930, 'right'),
           ('Price', 2180, 'right'), ('Ext Price', RIGHT, 'right')]
DESCRIPTION_RIGHT = 1840  # wrap edge for Item Description, leaving a gap before the right-aligned Qty


def tag_pdf(item, copies=1):
    """One bordered 4×3 tag per Letter sheet, inset for Brother hardware margins.

    One per sheet keeps cutting predictable and the first label in the same
    position for every job. Copies are actual sheets, never IPP's copies hint.
    """
    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas
    from .label_printing import tag_fields

    if not isinstance(copies, int) or not 1 <= copies <= 10:
        raise ValueError('Choose 1 to 10 copies.')
    output = BytesIO()
    page = canvas.Canvas(output, pagesize=letter)
    scale = 72 / 203
    left, top = 18, 774  # top-left, quarter-inch printable margin
    for _ in range(copies):
        page.rect(left, top - 216, 288, 216)
        for y, height, width, lines in tag_fields(item):
            size = height * scale
            for index, line in enumerate(lines):
                page.setFont('Helvetica', size)
                # Match the bounded ZPL font width, including unbroken item codes.
                text = page.beginText()
                desired = min(764 * scale, len(line) * width * scale)
                actual = stringWidth(line, 'Helvetica', size)
                text.setFont('Helvetica', size)
                text.setHorizScale(min(100, desired / actual * 100) if actual else 100)
                drawn = min(actual, desired)
                text.setTextOrigin(left + (288 - drawn) / 2,
                                   top - (y + height + index * (height + 4)) * scale)
                text.textOut(line)
                page.drawText(text)
        page.showPage()
    page.save()
    return output.getvalue()


class Pages:
    """Flowing layout onto Letter pages. Each page is a list of drawing steps."""

    def __init__(self, continuation=None):
        self.pages = []
        self.together = False
        self.continuation = continuation
        self.measure = ImageDraw.Draw(Image.new('L', (1, 1)))
        self.new_page(first=True)

    def new_page(self, first=False):
        self.steps = []
        self.pages.append(self.steps)
        self.y = TOP
        if testing_marks():
            self.banner()
        if not first and self.continuation:
            self.continuation(self)

    def width(self, text, size):
        return self.measure.textlength(text, font=font(size))

    def ensure(self, height):
        if self.together:
            return  # inside a block that was reserved as a whole (render_sales_order's closing block)
        if self.y + height > BOTTOM - 120:
            self.new_page()

    def reserve(self, height):
        """Start a block that must stay on one page: break now if `height` won't fit, then keep the block's own
        wraps from breaking it again. A block that fits on a still-single page may run down to the bottom margin
        (no 'Page N of M' label is printed on a one-page document); with testing marks on, the bottom bar's room
        is kept."""
        limit = BOTTOM - 120 if (len(self.pages) > 1 or testing_marks()) else BOTTOM - 30
        if self.y + height > limit:
            self.new_page()
        self.together = True

    def text(self, text, size=BODY, align='left', bold=False, x=None, advance=True):
        w = self.width(text, size)
        if x is None:
            x = {'left': LEFT, 'center': (PAGE_W - w) / 2, 'right': RIGHT - w}[align]
        elif align == 'right':
            x -= w
        self.steps.append(('text', x, self.y, text, size, bold))
        if advance:
            self.y += int(size * 1.25)

    def lines(self, text, size, width):
        out = []
        for paragraph in (text or '').splitlines():
            line = ''
            for word in paragraph.split():
                trial = f'{line} {word}'.strip()
                if line and self.width(trial, size) > width:
                    out.append(line)
                    line = word
                else:
                    line = trial
            out.append(line)
        return out

    def wrap(self, text, size=BODY, align='left', bold=False, x=None, width=None):
        for line in self.lines(text, size, width or (RIGHT - LEFT)):
            self.ensure(int(size * 1.25))
            self.text(line, size, align, bold, x=x)

    def rule(self, weight=4):
        self.steps.append(('rule', self.y, weight))
        self.y += weight + 14

    def banner(self):
        self.steps.append(('bar', self.y, 64))
        self.steps.append(('text-white', (PAGE_W - self.width(TEST_NOTICE, 44)) / 2, self.y + 8, TEST_NOTICE, 44))
        self.y += 90

    def images(self):
        result = []
        total = len(self.pages)
        for number, steps in enumerate(self.pages, 1):
            img = Image.new('L', (PAGE_W, PAGE_H), 255)
            draw = ImageDraw.Draw(img)
            footer = [('bar', BOTTOM - 64, 64), ('text-white', (PAGE_W - self.width(TEST_NOTICE, 44)) / 2, BOTTOM - 56, TEST_NOTICE, 44)] if testing_marks() else []
            if total > 1:
                label = f'Page {number} of {total}'
                footer.append(('text', RIGHT - self.width(label, 34), BOTTOM - 110, label, 34, False))
            for step in steps + footer:
                if step[0] == 'text':
                    _, x, y, text, size, bold = step
                    draw.text((x, y), text, font=font(size), fill=0, stroke_width=2 if bold else 0, stroke_fill=0)
                elif step[0] == 'text-white':
                    _, x, y, text, size = step
                    draw.text((x, y), text, font=font(size), fill=255, stroke_width=2, stroke_fill=255)
                elif step[0] == 'rule':
                    draw.rectangle([LEFT, step[1], RIGHT, step[1] + step[2] - 1], fill=0)
                elif step[0] == 'bar':
                    draw.rectangle([LEFT, step[1], RIGHT, step[1] + step[2]], fill=0)
                elif step[0] == 'image':
                    img.paste(step[3], (step[1], step[2]))
            result.append(img.point(lambda v: 0 if v < 160 else 255))  # crisp black/white for laser output
        return result


def column_header(p):
    for label, x, align in COLUMNS:
        p.text(label, 36, align, bold=True, x=x, advance=False)
    p.y += 48
    p.rule(3)


def customer_info_lines(doc):
    """Customer Info as QB prints it: name, company, street line(s), city/state/zip, every phone, email; blanks skipped.

    The block used to print only name, phone, mobile and email, so no sales order ever carried the customer's
    address (the QB sample it was copied from, #5367, happened to be a customer without one).
    """
    customer = doc.customer
    lines = [doc.customer_name]
    if customer:
        if customer.company and customer.company.strip() != (doc.customer_name or '').strip():
            lines.append(customer.company)
        lines += address_lines(customer)
        lines += customer.phone_numbers() + [customer.email]  # every phone, as receipts print them
    return [line.strip() for line in lines if line and line.strip()]


def address_lines(customer):
    """Street line(s) then 'City ST zip', the same shape receipts print (sales.printing.bill_to)."""
    return customer.address_lines()


def ship_to_lines(doc):
    """The order's shipping address when it is not simply the customer's own address; [] otherwise."""
    shipping = [line.strip() for line in (doc.shipping_address or '').splitlines() if line.strip()]
    if not shipping:
        return []
    own = [line.strip() for line in [doc.customer_name] + (address_lines(doc.customer) if doc.customer else []) if line and line.strip()]
    return [] if shipping == own else shipping


def render_sales_order(doc):
    def continuation(p):
        p.text(f'Sales Order #{doc.number} (continued)', 44, 'right', bold=True)
        p.y += 20
        column_header(p)

    p = Pages(continuation)
    now = timezone.localtime()
    top = p.y
    for label in [f"Printed: {now.strftime('%-m/%-d/%Y %-I:%M:%S %p')}",
                  f"Ordered: {timezone.localtime(doc.created_at).strftime('%-m/%-d/%Y')}",
                  f"Due: {doc.due_date.strftime('%-m/%-d/%Y')}" if doc.due_date else '',
                  f'Store: {business().short_code}' if business().short_code else '']:
        if label:
            p.text(label, BODY)
    p.y = top
    p.text(f'Sales Order #{doc.number}', 60, 'right', bold=True)
    p.y = top
    art = logo(860)
    if art:
        p.steps.append(('image', (PAGE_W - art.width) // 2, p.y, art))
        p.y += art.height + 50
    else:
        p.y = top + 150
        p.text(business().name, 120, 'center', bold=True)
        if business().tagline:
            p.text(business().tagline, 40, 'center')
        p.y += 60

    block = p.y
    p.text('Customer Info', 44, bold=True)
    info_width = 1100 - LEFT - 40  # the NOTES column starts at 1100
    for line in customer_info_lines(doc):
        p.wrap(line, BODY, width=info_width)
    if doc.customer_po:
        p.text(f'Customer PO: {doc.customer_po}', BODY)
    ship_to = ship_to_lines(doc)
    if ship_to:
        p.y += 20
        p.text('Ship To', 44, bold=True)
        for line in ship_to:
            p.wrap(line, BODY, width=info_width)
    left_end = p.y
    p.y = block
    if doc.notes:
        p.text('NOTES:', 44, bold=True, x=1100)
        for line in p.lines(doc.notes, 42, RIGHT - 1100):
            p.text(line, 42, bold=True, x=1100)
    p.y = max(p.y, left_end) + 50
    if doc.cashier:
        p.wrap(f'Cashier: {doc.cashier}', BODY)
    p.text(f'Order Status: {doc.get_status_display()}', 44, bold=True)
    p.y += 10
    column_header(p)

    for line in doc.lines.all():
        description = p.lines(line.description, 36, DESCRIPTION_RIGHT - 860)
        if line.discount_percent or line.discounted or line.discount_reason or line.discount_note:
            # The line's own % off its regular price and the reason, as the receipt prints it.
            description += p.lines(line_discount_text(line), 36, DESCRIPTION_RIGHT - 860)
        names = p.lines(line.item_name or line.sku, 36, 860 - 330 - 30) if (line.item_name or line.sku) else ['']
        height = max(len(description), len(names)) * 45 + 20
        p.ensure(min(height, BOTTOM - TOP - 400))
        row = p.y
        for cell, x, align in [(quantity(line.quantity), 1930, 'right'), (dollars(line.price), 2180, 'right'),
                               (dollars(line.amount), RIGHT, 'right')]:
            p.text(cell, 36, align, x=x, advance=False)
        for name in names:
            p.text(name, 36, x=330, advance=False)
            p.y += 45
        p.y = row
        for text in description:
            p.ensure(45)
            p.text(text, 36, x=860, advance=False)
            p.y += 45
        p.y = max(p.y, row + len(names) * 45) + 12
    p.rule(3)

    # Deposit History rows: (date, reference, amount, how paid). On an imported order the deposit QuickBooks POS took
    # comes first as one row, then whatever was recorded in this app; both are in Deposit Balance / Balance Due.
    payments = deposit_rows(doc)
    # The location sits on the tax line itself so it no longer reads as a Deposit History heading.
    tax_label = document_tax_label(doc, ' ')
    totals = [('Subtotal:', dollars(doc.subtotal), False)]
    if doc.discount:
        # Order-level % discount, taken off the subtotal before tax like the receipt's:
        # Subtotal - Discount + Tax = TOTAL. The percent and reason sit on the label.
        totals.append((discount_label(doc), '- ' + dollars(doc.discount), False))
    totals += [(tax_label, '+ ' + dollars(doc.tax or Decimal('0')), False),
               ('TOTAL:', dollars(doc.total), True), ('Deposit Balance:', dollars(doc.unused_payments), True),
               ('Balance Due:', dollars(doc.balance_due), False)]
    from . import accounts
    # Review finding 7: an order's app payments by Account show whether or not the order is read-only now (a closed
    # QuickBooks order keeps the account charges and refunds recorded here; QB's own deposits are never OrderPayments).
    charged = accounts.order_charged(doc)
    if charged and doc.customer_id:
        # Deposits paid by Account: show what went on the charge account and the customer's balance now.
        totals += [('Charged to Account:', dollars(charged), False), ('Account Balance:', dollars(doc.customer.account_balance), True)]

    # reserve only what the closing block really needs (its payment rows, totals rows, the signature when
    # something was charged, the thank-you, footer and store block), so a short order stays on one page. The old
    # fixed reserve (room for 7 payment rows + 500 px) pushed any order past ~7 lines onto a second page with a third
    # of page 1 empty. Totals, signature and footer still move together: the footer never prints alone.
    p.reserve(min(closing_height(p, payments, totals, charged), BOTTOM - TOP - 200))
    p.y += 40
    block = p.y
    if payments:
        p.text('Deposit History', 40, bold=True, x=700, advance=False)
        p.y += 52
        for label, x, align in [('Date', LEFT, 'left'), ('Reference', 520, 'left'), ('Amount', 1080, 'right'), ('Payment', 1120, 'left')]:
            p.text(label, 36, align, bold=True, x=x, advance=False)
        p.y += 46
        p.steps.append(('rule', p.y, 3))
        p.y += 12
        for when, reference, amount, how in payments:
            for cell, x, align in [(when, LEFT, 'left'), (reference[:28], 520, 'left'), (dollars(amount), 1080, 'right'), (how, 1120, 'left')]:
                p.text(cell, 36, align, x=x, advance=False)
            p.y += 48
    left_end = p.y
    p.y = block
    label_x, value_x = 2160, RIGHT
    for label, value, bold in totals:
        p.text(label, BODY, 'right', bold=bold, x=label_x, advance=False)
        p.text(value, BODY, 'right', bold=bold, x=value_x, advance=False)
        p.y += 48
    p.y = max(p.y, left_end) + 40
    if charged > 0:
        p.text('Signature ________________________________', BODY, x=LEFT + 300)
        p.y += 10
        p.wrap(accounts.SIGNATURE_TEXT, 34, 'center', width=RIGHT - LEFT - 400)
        p.y += 40
    shop = business()
    if shop.receipt_footer:
        p.text(shop.receipt_footer, BODY, 'center')
    p.y += 24
    if shop.order_policy:
        p.wrap(shop.order_policy, 38, 'center', width=RIGHT - LEFT - 100)
    p.y += 30
    for line in shop.order_lines():
        p.text(line, BODY, 'center')
    return p.images()


def discount_label(doc):
    """'Discount (5 % Staff pick):' - the order discount's percent and reason/note as the receipt words them; a dollar
    order discount (percent 0) reads 'Discount ($50.00 Staff pick):'."""
    from .models import discount_text
    parts = []
    if doc.discount_percent:
        parts.append(f'{quantity(doc.discount_percent)} %')
    elif doc.discount:
        parts.append(dollars(doc.discount))
    detail = discount_text(doc.discount_reason, doc.discount_note)
    if detail:
        parts.append(detail)
    return f"Discount ({' '.join(parts)}):" if parts else 'Discount:'


def line_discount_text(line):
    """'Discount: 10 % off $1,209.95 - Sale' for a line with its own % off; '$35.00 off $484.95 - Sale' for a
    dollar discount off each unit (also a price typed below regular, which QB treats the same); 'regular $X' when
    the price below regular has fractions of a cent; reason/note alone otherwise."""
    from .models import discount_text
    parts = []
    if line.discount_percent:
        parts.append(f'{quantity(line.discount_percent)} % off {dollars(line.original_price)}' if line.original_price is not None else f'{quantity(line.discount_percent)} % off')
    elif line.dollars_off is not None:
        parts.append(f'{dollars(line.dollars_off)} off {dollars(line.original_price)}')
    elif line.discounted:
        parts.append(f'regular {dollars(line.original_price)}')
    detail = discount_text(line.discount_reason, line.discount_note)
    if detail:
        parts.append(detail)
    return 'Discount: ' + ' - '.join(parts)


def deposit_rows(doc):
    """The sales order's Deposit History as printed: [(date text, reference, amount, how paid)]. QuickBooks' deposit on
    an imported order is one row ('Deposit taken in QuickBooks POS', dated by its latest QB deposit receipt, the
    order's date when none is linked), followed by the payments recorded in this app."""
    rows = []
    qb = doc.qb_deposit if doc.kind == 'order' else None
    if qb:
        # The Reference column holds 28 characters; the count says how many QB deposit receipts the row sums.
        reference = 'Deposit taken in QB POS' + (f" ({qb['count']})" if qb['count'] > 1 else '')
        rows.append((qb['date'].strftime('%-m/%-d/%Y'), reference, qb['amount'], 'QuickBooks'))
    for payment in doc.payments.all():
        if not payment.is_tender:
            continue   # a sales tax adjustment is not a deposit
        # a cash payment with an Amount received prints QB's way, the cash handed over and the change.
        how = f'Cash {dollars(payment.tendered)} · Change {dollars(payment.change_given)}' if payment.change_given is not None else payment.label
        rows.append((timezone.localtime(payment.created_at).strftime('%-m/%-d/%Y'), payment.reference, payment.amount, how))
    return rows


def closing_height(p, payments, totals, charged):
    """Height of everything after the line table, measured the way render_sales_order draws it: the gap, the
    taller of the Deposit History rows and the totals rows, the signature block when something was charged to the
    account, 'Thank you', the wrapped footer and the store block."""
    from . import accounts
    height = 40
    payments_height = (52 + 46 + 12 + 48 * len(payments)) if payments else 0
    height += max(payments_height, 48 * len(totals)) + 40
    if charged > 0:
        height += int(BODY * 1.25) + 10 + len(p.lines(accounts.SIGNATURE_TEXT, 34, RIGHT - LEFT - 400)) * int(34 * 1.25) + 40
    shop = business()
    height += (int(BODY * 1.25) if shop.receipt_footer else 0) + 24
    height += (len(p.lines(shop.order_policy, 38, RIGHT - LEFT - 100)) * int(38 * 1.25) if shop.order_policy else 0) + 30
    height += len(shop.order_lines()) * int(BODY * 1.25)
    return height


def render_letter_test_page(printer):
    p = Pages()
    p.y += 40
    p.text(business().name, 120, 'center', bold=True)
    p.text('Printer test - not a sales order', 50, 'center')
    p.y += 40
    p.text(printer.name, 70, 'center', bold=True)
    for line in [printer.model, printer.location, f'{printer.host}:{printer.port}', printer.mac_address,
                 timezone.localtime().strftime('%-m/%-d/%Y %-I:%M:%S %p'), 'Sent from the app server as PWG raster over IPP']:
        if line:
            p.text(line, 46, 'center')
    p.y += 60
    p.steps.append(('rule', p.y, 4))
    p.y += 20
    for inch in range(8):  # inch marks from the left margin: checks scaling (should measure exactly 1 inch apart)
        p.text(f'{inch}"', 34, x=LEFT + inch * DPI - (0 if inch == 0 else 20), advance=False)
        p.steps.append(('rule-v', LEFT + inch * DPI, p.y + 50))
    p.y += 120
    p.text('Marks are 1 inch apart when printed at 100% scale.', 38, 'center')
    pages = p.images()
    draw = ImageDraw.Draw(pages[0])
    for step in p.pages[0]:
        if step[0] == 'rule-v':
            draw.rectangle([step[1] - 2, step[2], step[1] + 2, step[2] + 60], fill=0)
    return pages


def pwg_raster(pages, color=False):
    """Encode Letter pages as PWG raster (PWG 5102.4) with its PackBits-style compression.

    Gray (default): 8-bit sgray, one byte per pixel. color=True (color Letter printers): 24-bit srgb, three
    bytes per pixel; gray pages in a color job are converted. Runs and literals count pixels, not bytes."""
    mode, size, space = ('RGB', 3, 19) if color else ('L', 1, 18)
    out = bytearray(b'RaS2')
    for img in pages:
        if img.mode != mode:
            img = img.convert(mode)
        header = bytearray(1796)
        header[0:9] = b'PwgRaster'
        header[1732:1750] = b'na_letter_8.5x11in'

        def put(offset, value):
            struct.pack_into('>I', header, offset, value)

        put(276, DPI); put(280, DPI)          # HWResolution
        put(340, 1)                          # NumCopies: always 1; extra copies are sent as repeated pages (copy_sets)
        put(352, 612); put(356, 792)         # PageSize in points
        put(372, PAGE_W); put(376, PAGE_H)   # Width, Height
        put(384, 8); put(388, 8 * size); put(392, PAGE_W * size)  # BitsPerColor, BitsPerPixel, BytesPerLine
        put(400, space)                      # ColorSpace: 18 sgray, 19 srgb
        put(420, size)                       # NumColors
        put(452, len(pages))                 # TotalPageCount
        put(456, 1); put(460, 1)             # CrossFeedTransform, FeedTransform
        put(480, 0xFFFFFF)                   # AlternatePrimary (white)
        out += header
        data = img.tobytes()
        line = PAGE_W * size
        rows = [data[r * line:(r + 1) * line] for r in range(PAGE_H)]
        r = 0
        while r < PAGE_H:
            row = rows[r]
            repeat = 0
            while repeat < 255 and r + repeat + 1 < PAGE_H and rows[r + repeat + 1] == row:
                repeat += 1
            out.append(repeat)
            out += encode_row(row, size)
            r += repeat + 1
    return bytes(out)


def encode_row(row, size=1):
    white = b'\xff' * size
    if size == 1:
        pixels = row
    else:
        pixels = [row[i:i + size] for i in range(0, len(row), size)]
    runs = [(value, len(list(group))) for value, group in groupby(pixels)]
    out = bytearray()
    literal = []

    def unit(value):
        return bytes([value]) if size == 1 else value

    def flush():
        while literal:
            chunk = literal[:128]
            del literal[:128]
            if len(chunk) == 1:
                out.append(0)
            else:
                out.append(257 - len(chunk))
            out.extend(b''.join(unit(v) for v in chunk))

    for index, (value, count) in enumerate(runs):
        if index == len(runs) - 1 and unit(value) == white:
            flush()
            out.append(128)  # rest of the line is white
            return bytes(out)
        if count == 1:
            literal.append(value)
            continue
        flush()
        while count:
            chunk = min(count, 128)
            out += bytes([chunk - 1]) + unit(value)
            count -= chunk
    flush()
    return bytes(out)


def has_color(img):
    """True when an RGB page holds any non-gray pixel (cheap, done in C by Pillow)."""
    from PIL import ImageChops
    if img.mode != 'RGB':
        return False
    r, g, b = img.split()
    return bool(ImageChops.difference(r, g).getbbox() or ImageChops.difference(g, b).getbbox())


def ipp_attribute(tag, name, value):
    value = value if isinstance(value, bytes) else value.encode()
    return struct.pack('>BH', tag, len(name)) + name.encode() + struct.pack('>H', len(value)) + value


def ipp_request(printer, operation, attributes=b'', job_attributes=b'', data=b'', timeout=10):
    uri = f'ipp://{printer.host}/ipp/print'
    body = (struct.pack('>BBHI', 2, 0, operation, 1) + b'\x01' + ipp_attribute(0x47, 'attributes-charset', 'utf-8')
            + ipp_attribute(0x48, 'attributes-natural-language', 'en') + ipp_attribute(0x45, 'printer-uri', uri)
            + attributes + (b'\x02' + job_attributes if job_attributes else b'') + b'\x03' + data)
    request = urllib.request.Request(f'http://{printer.host}:{printer.port}/ipp/print', data=body, headers={'Content-Type': 'application/ipp'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = response.read()
    except (OSError, urllib.error.URLError):
        raise PrintError(f'Could not reach {printer.name}. Check that it is on and connected.')
    if len(reply) < 8:
        raise PrintError(f'{printer.name} sent an unexpected reply.')
    return struct.unpack('>H', reply[2:4])[0], parse_attributes(reply)


def parse_attributes(reply):
    attributes, name, i = {}, '', 8
    while i < len(reply):
        tag = reply[i]
        i += 1
        if tag < 0x10:
            if tag == 0x03:
                break
            continue
        length = struct.unpack('>H', reply[i:i + 2])[0]
        i += 2
        name = reply[i:i + length].decode(errors='replace') or name
        i += length
        length = struct.unpack('>H', reply[i:i + 2])[0]
        i += 2
        value = reply[i:i + length]
        i += length
        value = struct.unpack('>i', value)[0] if tag in (0x21, 0x23) and length == 4 else value.decode(errors='replace')
        attributes.setdefault(name, []).append(value)
    return attributes


PROBLEMS = {'media-empty': 'out of paper', 'media-jam': 'paper jam', 'toner-empty': 'out of toner', 'door-open': 'cover open',
            'cover-open': 'cover open', 'input-tray-missing': 'paper tray missing', 'marker-supply-empty': 'out of toner'}


def letter_status(printer):
    try:
        status, attributes = ipp_request(printer, 0x000B, timeout=5)
    except PrintError:
        return {'ok': False, 'status': 'Not reachable'}
    reasons = [r.removesuffix('-error').removesuffix('-warning').removesuffix('-report') for r in attributes.get('printer-state-reasons', []) if r != 'none']
    problems = [PROBLEMS[r] for r in reasons if r in PROBLEMS]
    state = {3: 'Ready', 4: 'Printing', 5: 'Stopped'}.get((attributes.get('printer-state') or [0])[0], 'Online')
    if problems:
        return {'ok': False, 'status': ', '.join(dict.fromkeys(problems)).capitalize()}
    if 'toner-low' in reasons:
        return {'ok': True, 'status': f'{state} (toner low)'}
    return {'ok': state != 'Stopped', 'status': state}


def copy_sets(pages, copies):
    """The page list repeated `copies` times as collated sets (1..n, 1..n,...), ready for one raster job.

    The printers printed one copy however many were asked for. IPP Everywhere 1.1 (PWG 5100.14, section 5.3
    table 5 note 2 and section 5.8 table 12 note 2) requires "copies" / "copies-supported" only for application/pdf
    and image/jpeg documents; for PWG raster, which we send, the printer may print the stream exactly once
    (the Brothers do). CUPS handles such printers the same way ("cupsManualCopies": the filters emit the
    copies), so the copies become pages here and the job asks for copies=1.
    """
    pages = list(pages)
    return pages * max(1, int(copies))


# Other names printers use for the same tray (PWG 5100.7 media-source keywords). If the printer does not list the
# chosen keyword in media-source-supported, the first alias it does list is sent instead.
MEDIA_SOURCE_ALIASES = {'tray-1': ['main', 'top'], 'tray-2': ['bottom', 'lower'], 'manual': ['by-pass-tray', 'alternate', 'side']}


def ipp_collection(name, members):
    """IPP collection value (RFC 8010 3.1.6). members: [(member name, tag, value or nested member list)]."""
    def body(items):
        out = b''
        for member, tag, value in items:
            out += ipp_attribute(0x4A, '', member)
            if tag == 0x34:
                out += ipp_attribute(0x34, '', b'') + body(value) + ipp_attribute(0x37, '', b'')
            else:
                out += ipp_attribute(tag, '', value)
        return out
    return ipp_attribute(0x34, name, b'') + body(members) + ipp_attribute(0x37, '', b'')


def media_source(printer):
    """The tray keyword to send for this printer, or '' for the printer's own choice."""
    wanted = getattr(printer, 'media_source', '') or ''
    if not wanted:
        return ''
    try:
        status, attributes = ipp_request(printer, 0x000B, ipp_attribute(0x44, 'requested-attributes', 'media-source-supported'), timeout=5)
    except PrintError:
        return wanted
    supported = attributes.get('media-source-supported') or []
    if status > 0x00FF or not supported or wanted in supported:
        return wanted
    return next((alias for alias in MEDIA_SOURCE_ALIASES.get(wanted, []) if alias in supported), wanted)


def send_letter(printer, pages, job_name, copies=1):
    """One IPP Print-Job of PWG raster pages. Color printers get a color raster only when a page holds
    color; everything else prints monochrome, as before. A chosen paper tray goes in media-col/media-source."""
    pages = list(pages)
    color = bool(getattr(printer, 'color', False)) and any(has_color(page) for page in pages)
    job = (ipp_attribute(0x42, 'requesting-user-name', 'freedomshop') + ipp_attribute(0x42, 'job-name', job_name[:100])
           + ipp_attribute(0x49, 'document-format', 'image/pwg-raster'))
    tray = media_source(printer)
    if tray:
        media = ipp_collection('media-col', [('media-size', 0x34, [('x-dimension', 0x21, struct.pack('>i', 21590)),
                                                                     ('y-dimension', 0x21, struct.pack('>i', 27940))]),
                                             ('media-source', 0x44, tray)])
    else:
        media = ipp_attribute(0x44, 'media', 'na_letter_8.5x11in')
    job_attributes = (ipp_attribute(0x21, 'copies', struct.pack('>i', 1)) + media
                      + ipp_attribute(0x44, 'sides', 'one-sided') + ipp_attribute(0x44, 'print-color-mode', 'color' if color else 'monochrome'))
    status, attributes = ipp_request(printer, 0x0002, job, job_attributes, pwg_raster(copy_sets(pages, copies), color=color), timeout=60)
    if status > 0x00FF:
        message = (attributes.get('status-message') or [''])[0]
        raise PrintError(f'{printer.name} refused the job (IPP status 0x{status:04x}){": " + message if message else ""}.')
    return printer.name


def receipt_on_letter(receipt):
    """Place a 72mm receipt strip at true size on Letter pages, splitting long receipts across pages."""
    scale = DPI / 203
    strip = receipt.convert('L').resize((round(receipt.width * scale), round(receipt.height * scale)), Image.LANCZOS)
    strip = strip.point(lambda v: 0 if v < 160 else 255)
    usable = BOTTOM - TOP
    x = (PAGE_W - strip.width) // 2
    pages = []
    for top in range(0, strip.height, usable):
        page = Image.new('L', (PAGE_W, PAGE_H), 255)
        page.paste(strip.crop((0, top, strip.width, min(top + usable, strip.height))), (x, TOP))
        pages.append(page)
    return pages
