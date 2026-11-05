# SPDX-License-Identifier: AGPL-3.0-or-later
"""Furniture Set Tag contents and the Letter printout, laid out like a shop's QB POS set sign ( photo,
the import parity checks).

Money follows the whole-cents rule: unit price rounded half-up to cents, extension = qty x price rounded half-up, tax =
subtotal x rate rounded half-up, as QB POS does.
"""
import io
from decimal import ROUND_HALF_UP, Decimal

from sales.printing import logo_path, testing_marks
from shop_settings.config import business

CENT = Decimal('0.01')
TEST_NOTICE = 'TESTING - NOT A VALID PRICE TAG'
GREEN = '#1B6B3A'


def cents(value):
    return (value or Decimal('0')).quantize(CENT, rounding=ROUND_HALF_UP)


def dollars(value):
    return f'${value:,.2f}' if value >= 0 else f'-${-value:,.2f}'


def qty_text(value):
    text = f'{value:,.3f}'.rstrip('0').rstrip('.')
    return text or '0'


def rate_text(rate):
    return f'{rate:f}'.rstrip('0').rstrip('.')


def build(tag_set):
    """Plain data for the screen and the printout, priced from the Item List now."""
    from shop_settings.config import tax
    config = tax()
    rows = []
    taxed = {}
    for line in tag_set.lines.select_related('item'):
        item = line.item
        name = (item.name or item.sku) if item else line.item_name
        description = item.description if item else line.description
        price = cents(item.price) if item else Decimal('0.00')
        ext = cents(line.quantity * price)
        rate = config.line_rate if item is None or item.tax_rate is None else item.tax_rate
        if rate and item is not None:
            taxed[rate] = taxed.get(rate, Decimal('0')) + ext
        rows.append(dict(line=line, item=item, item_name=name, description=description, price=price, quantity=line.quantity, ext=ext,
                         missing=item is None, inactive=bool(item and not item.active), no_price=bool(item and item.price is None)))
    subtotal = sum((r['ext'] for r in rows), Decimal('0.00'))
    tax = sum((cents(amount * rate / 100) for rate, amount in taxed.items()), Decimal('0.00'))
    if len(taxed) == 1:
        tax_label = f'{config.default_location.receipt_label}   {rate_text(next(iter(taxed)))} % Tax:'
    else:
        tax_label = 'Tax:'
    return dict(rows=rows, pieces=sum((r['quantity'] for r in rows), Decimal('0')), subtotal=subtotal, tax=tax, tax_label=tax_label, total=subtotal + tax,
                tagline=[part.strip() for part in (tag_set.tagline or '').splitlines() if part.strip()])


SET_FONT_SCALE = 1.2   # every font on the set tag 20 % larger (logo size unchanged)


def pdf_bytes(tag_set, copies=1):
    """Letter portrait: logo and store lines centered, item table, rule, totals at the right, green tagline."""
    from reportlab.lib.colors import HexColor, black
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph
    from xml.sax.saxutils import escape

    data = build(tag_set)
    width, height = letter
    left, right = 0.55 * inch, width - 0.55 * inch
    col_name, col_desc, col_price, col_qty, col_ext = left, 2.95 * inch, 6.15 * inch, 6.75 * inch, right
    name_style = ParagraphStyle('name', fontName='Helvetica', fontSize=9 * SET_FONT_SCALE, leading=11 * SET_FONT_SCALE)
    desc_style = ParagraphStyle('desc', fontName='Helvetica', fontSize=9 * SET_FONT_SCALE, leading=11 * SET_FONT_SCALE)
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=letter, pageCompression=1)
    pdf.setTitle(f'Furniture Set Tag - {tag_set.name}')
    pdf.setAuthor(business().name)
    marks = testing_marks()
    for _ in range(max(1, copies)):
        y = height - 0.55 * inch
        if marks:
            pdf.setFont('Helvetica-Bold', 10)
            pdf.drawCentredString(width / 2, height - 0.35 * inch, TEST_NOTICE)
        logo_file = logo_path()
        if logo_file is not None:
            logo_w = 2.45 * inch
            logo_h = logo_w * 650 / 1213
            pdf.drawImage(ImageReader(str(logo_file)), (width - logo_w) / 2, y - logo_h, logo_w, logo_h, mask='auto')
            y -= logo_h + 0.12 * inch
        else:
            pdf.setFont('Helvetica-Bold', 26 * SET_FONT_SCALE)
            pdf.drawCentredString(width / 2, y - 26 * SET_FONT_SCALE, business().name)
            y -= 40
        pdf.setFont('Helvetica', 9.5 * SET_FONT_SCALE)
        for text in business().receipt_lines()[1:]:   # address and phone, as on the receipt (the logo carries the name)
            pdf.drawCentredString(width / 2, y - 9 * SET_FONT_SCALE, text)
            y -= 12 * SET_FONT_SCALE
        y -= 0.62 * inch
        pdf.setFont('Helvetica-Bold', 10 * SET_FONT_SCALE)
        pdf.drawString(col_name, y, 'Item Name')
        pdf.drawString(col_desc, y, 'Description')
        pdf.drawRightString(col_price, y, 'Price')
        pdf.drawRightString(col_qty, y, 'Qty')
        pdf.drawRightString(col_ext, y, 'Ext Price')
        pdf.setLineWidth(0.6)
        pdf.line(col_name, y - 3, col_ext, y - 3)
        y -= 15 * SET_FONT_SCALE
        for row in data['rows']:
            name = Paragraph(escape(row['item_name']), name_style)
            desc = Paragraph('<br/>'.join(escape(part) for part in row['description'].splitlines()) or '&nbsp;', desc_style)
            _, name_h = name.wrap(col_desc - col_name - 10, 400)
            _, desc_h = desc.wrap(col_price - col_desc - 60, 400)
            top = y + 9 * SET_FONT_SCALE
            name.drawOn(pdf, col_name, top - name_h)
            desc.drawOn(pdf, col_desc, top - desc_h)
            pdf.setFont('Helvetica', 9.5 * SET_FONT_SCALE)
            pdf.drawRightString(col_price, y, dollars(row['price']))
            pdf.drawRightString(col_qty, y, qty_text(row['quantity']))
            pdf.drawRightString(col_ext, y, dollars(row['ext']))
            y -= max(name_h, desc_h, 11 * SET_FONT_SCALE) + 7
        rule_y = min(y - 0.2 * inch, height * 0.44)
        pdf.setLineWidth(0.6)
        pdf.line(left - 0.05 * inch, rule_y, right, rule_y)
        y = rule_y - 0.22 * inch
        label_x, value_x = right - 1.25 * inch, right
        pdf.setFont('Helvetica', 9.5 * SET_FONT_SCALE)
        pdf.drawRightString(label_x, y, 'Subtotal:')
        pdf.drawRightString(value_x, y, dollars(data['subtotal']))
        y -= 12 * SET_FONT_SCALE
        pdf.drawRightString(label_x, y, data['tax_label'])
        pdf.drawRightString(value_x, y, '+ ' + dollars(data['tax']))
        y -= 12 * SET_FONT_SCALE
        pdf.setFont('Helvetica-Bold', 10 * SET_FONT_SCALE)
        pdf.drawRightString(label_x, y, 'SET TOTAL:')
        pdf.drawRightString(value_x, y, dollars(data['total']))
        y = min(y - 0.85 * inch, height * 0.27)
        pdf.setFillColor(HexColor(GREEN))
        pdf.setFont('Helvetica-Bold', 15 * SET_FONT_SCALE)
        for text in data['tagline']:
            pdf.drawCentredString(width / 2, y, text)
            y -= 20 * SET_FONT_SCALE
        pdf.setFillColor(black)
        if marks:
            pdf.setFont('Helvetica-Bold', 10)
            pdf.drawCentredString(width / 2, 0.35 * inch, TEST_NOTICE)
        pdf.showPage()
    pdf.save()
    return stream.getvalue()
