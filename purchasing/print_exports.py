# SPDX-License-Identifier: AGPL-3.0-or-later
"""Printable Purchase Order and Receiving Voucher (Letter portrait PDF), with the approved layout.

Layout: store header (logo, address, phone, fax) and document box; full vendor block; lines with Item Name, the full
Item Description (wrapped, never truncated), quantity, cost and extension; a totals row with total quantity and total
cost; instructions, terms and payment due when filled in. Received/Due columns are screen-only and are not printed.

The PDF is the print master: Direct Print rasterizes it for the Brother Letter printers (reports.direct_print).
Money follows the whole-cents rule: unit cost rounded half-up to cents, extension = qty x cost, rounded half-up.
"""
import io
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from sales.printing import logo_path, testing_marks
from shop_settings.config import business

CENT = Decimal('0.01')
BRAND = '#1B4B34'
# QB's Ship Date is printed as the Preferred Pickup Date, emphasized, with the Alternate below it.
PREFERRED = 'Preferred Pickup Date'
ALTERNATE = 'Alternate Pickup Date'
TEST_NOTICE = {'po': 'TESTING - NOT A VALID PURCHASE ORDER', 'voucher': 'TESTING - NOT A VALID RECEIVING VOUCHER'}


def cents(value):
    return (value or Decimal('0')).quantize(CENT, rounding=ROUND_HALF_UP)


def extension(line):
    return line.amount  # qty x cost in whole cents, or QB's recorded line total on imported lines


def dollars(value):
    return f'${value:,.2f}' if value >= 0 else f'-${-value:,.2f}'


def qty(value):
    text = f'{value:,.3f}'.rstrip('0').rstrip('.')
    return text if text not in ('', '-0') else '0'


def day(value):
    if value is None:
        return ''
    if hasattr(value, 'tzinfo'):
        value = timezone.localtime(value)
    return f'{value:%-m/%-d/%Y}'


def vendor_lines(doc):
    """Vendor block text lines; empty fields are left out. Falls back to the saved vendor name snapshot."""
    vendor = doc.vendor
    if vendor is None:
        return [doc.vendor_name or 'Vendor not on file']
    lines = [vendor.company or doc.vendor_name]
    if vendor.contact:
        lines.append(f'Attn: {vendor.contact}')
    lines += [part for part in (vendor.street or '').splitlines() if part.strip()]
    city = ', '.join(p for p in [vendor.city, ' '.join(p for p in [vendor.state, vendor.postal_code] if p)] if p)
    if city:
        lines.append(city)
    for label, value in (('Phone', vendor.phone), ('Fax', vendor.fax), ('Email', vendor.email), ('Account #', vendor.account_number)):
        if value:
            lines.append(f'{label}: {value}')
    return lines


def document_fields(doc):
    """(label, value) pairs for the document box; blank values are left out."""
    if doc.kind == 'po':
        rows = [('PO #', doc.number), ('Order Date', day(doc.created_at)), (PREFERRED, day(doc.ship_date)),
                (ALTERNATE, day(doc.alternate_pickup_date)), ('Status', doc.get_status_display())]
    else:
        rows = [('Voucher #', doc.number), ('Received', day(doc.posted_at)) if doc.posted_at else ('Created', day(doc.created_at)),
                ('Status', doc.get_status_display()), ('Vendor Invoice #', doc.invoice_number),
                ('Invoice Date', day(doc.invoice_date))]
        if doc.source_po_id:
            rows.append(('From PO', doc.source_po.number))
    if doc.clerk:
        rows.append(('Associate', doc.clerk))
    # both pickup dates always print on a PO; a blank one prints as an empty slot the builder can see.
    return [(label, value) for label, value in rows if value or label in (PREFERRED, ALTERNATE)]


def build(doc):
    """Plain data for the printout, also used by tests."""
    lines = list(doc.lines.all())
    rows = [dict(name=line.item_name or line.sku or '', description=line.description or '', quantity=line.quantity,
                 cost=cents(line.cost), amount=extension(line)) for line in lines]
    total_qty = sum((row['quantity'] for row in rows), Decimal('0'))
    total_cost = sum((row['amount'] for row in rows), Decimal('0'))
    freight = cents(doc.freight) if doc.freight else Decimal('0')
    orders = [f'{o.number} · {o.customer_name}' for o in doc.customer_orders] if doc.pk else []
    return dict(title='Purchase Order' if doc.kind == 'po' else 'Receiving Voucher', rows=rows, total_qty=total_qty,
                total_cost=total_cost, freight=freight, grand_total=total_cost + freight, vendor=vendor_lines(doc),
                fields=document_fields(doc), orders=orders,
                footer=[(label, value.strip()) for label, value in (('Instructions', doc.notes or ''), ('Terms', doc.terms or ''),
                                                                     ('Payment Due', day(doc.payment_due))) if value.strip()])


def pdf_bytes(doc):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    shop = business()
    store_name, store_address = shop.legal_name or shop.name, shop.address_lines()

    data = build(doc)
    marks = testing_marks()
    brand = colors.HexColor(BRAND)
    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'], fontSize=9, leading=11)
    small = ParagraphStyle('small', parent=body, fontSize=8.5, leading=10.5)
    bold = ParagraphStyle('bold', parent=body, fontName='Helvetica-Bold')
    right = ParagraphStyle('right', parent=body, alignment=TA_RIGHT)
    bold_right = ParagraphStyle('bold_right', parent=bold, alignment=TA_RIGHT)
    head = ParagraphStyle('head', parent=bold, textColor=colors.white, fontSize=8.5)
    head_right = ParagraphStyle('head_right', parent=head, alignment=TA_RIGHT)
    title = ParagraphStyle('title', parent=bold, fontSize=20, leading=24, textColor=brand, alignment=TA_RIGHT)
    store = ParagraphStyle('store', parent=body, fontSize=9.5, leading=12)

    def p(text, style=body):
        text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
        return Paragraph(text, style)

    class NumberedCanvas(pdf_canvas.Canvas):
        """Two-pass canvas so every page can say "Page N of M"."""
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont('Helvetica', 8)
                self.drawString(0.5 * inch, 0.35 * inch, f'{store_name} · {data["title"]} {doc.number}')
                self.drawRightString(letter[0] - 0.5 * inch, 0.35 * inch, f'Page {self._pageNumber} of {total}')
                if marks:
                    self.setFont('Helvetica-Bold', 9)
                    self.drawCentredString(letter[0] / 2, letter[1] - 0.32 * inch, TEST_NOTICE[doc.kind])
                    self.drawCentredString(letter[0] / 2, 0.55 * inch, TEST_NOTICE[doc.kind])
                super().showPage()
            super().save()

    stream = io.BytesIO()
    pdf = SimpleDocTemplate(stream, pagesize=letter, leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.75 * inch,
                            title=f'{data["title"]} {doc.number}', author=store_name)
    width = pdf.width

    # Header: logo + store block (left), title + document box (right).
    store_cells = []
    logo_file = logo_path()
    if logo_file is not None:
        art = Image(str(logo_file), width=1.55 * inch, height=1.55 * inch * 650 / 1213)
        art.hAlign = 'LEFT'
        store_cells.append(art)
        store_cells.append(Spacer(1, 4))
    store_cells.append(Paragraph('<b>' + store_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') + '</b>', store))
    store_cells += [p(line, store) for line in store_address]
    store_cells += [p(f'Phone {shop.phone}', store)] if shop.phone else []
    store_cells += [p(f'Fax {shop.fax}', store)] if shop.fax else []
    value_style = ParagraphStyle('v', parent=small, fontName='Helvetica-Bold', alignment=TA_RIGHT)
    pickup_label = ParagraphStyle('pl', parent=small, fontName='Helvetica-Bold', fontSize=10, leading=12, textColor=brand)
    pickup_value = ParagraphStyle('pv', parent=value_style, fontSize=12, leading=14)
    box_rows, box_style = [], [('BOX', (0, 0), (-1, -1), 0.75, brand), ('LINEBELOW', (0, 0), (-1, -2), 0.25, colors.HexColor('#C9D3CC')),
                               ('TOPPADDING', (0, 0), (-1, -1), 2.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]
    for index, (label, value) in enumerate(data['fields']):
        if label == PREFERRED:
            # Bigger bold text on a shaded band with a heavy frame, so the builder's eye goes there first.
            box_rows.append([p(label, pickup_label), p(value, pickup_value)])
            box_style += [('BACKGROUND', (0, index), (-1, index), colors.HexColor('#FFF2B3')),
                          ('BOX', (0, index), (-1, index), 1.6, colors.black),
                          ('TOPPADDING', (0, index), (-1, index), 5), ('BOTTOMPADDING', (0, index), (-1, index), 5)]
        elif label == ALTERNATE:
            box_rows.append([p(label, ParagraphStyle('al', parent=small, fontName='Helvetica-Bold')), p(value, ParagraphStyle('av', parent=value_style, fontSize=10.5, leading=12.5))])
            box_style += [('TOPPADDING', (0, index), (-1, index), 5), ('BOTTOMPADDING', (0, index), (-1, index), 5),
                          ('LINEBELOW', (1, index), (1, index), 0.75, colors.black)]
        else:
            box_rows.append([p(label, small), p(value, value_style)])
    box = Table(box_rows, colWidths=[1.9 * inch, 1.05 * inch])
    box.setStyle(TableStyle(box_style))
    header = Table([[store_cells, [p(data['title'].upper(), title), Spacer(1, 6), box]]],
                   colWidths=[width - 3.0 * inch, 3.0 * inch])
    header.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
                                ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))

    # Vendor block (and Ship To for POs: the store).
    vendor_cell = [p('VENDOR', ParagraphStyle('lbl', parent=bold, fontSize=8, textColor=brand))]
    vendor_cell += [p(line, bold if i == 0 else body) for i, line in enumerate(data['vendor'])]
    ship_cell = [p('SHIP TO' if doc.kind == 'po' else 'RECEIVED AT', ParagraphStyle('lbl2', parent=bold, fontSize=8, textColor=brand)),
                 p(store_name, bold)] + [p(line) for line in store_address] + ([p(f'Phone: {shop.phone}')] if shop.phone else [])
    parties = Table([[vendor_cell, ship_cell]], colWidths=[width * 0.55, width * 0.45])
    parties.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOX', (0, 0), (0, 0), 0.5, colors.grey),
                                 ('BOX', (1, 0), (1, 0), 0.5, colors.grey), ('LEFTPADDING', (0, 0), (-1, -1), 7),
                                 ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))

    story = [header, Spacer(1, 10), parties, Spacer(1, 10)]
    if data['orders']:
        story += [p('For customer order' + ('s' if len(data['orders']) > 1 else '') + ': ' + '; '.join(data['orders']), small), Spacer(1, 6)]

    # Lines. Received/Due are not printed; the freed width goes to Item Description.
    name_style = ParagraphStyle('name', parent=body, fontSize=8, leading=10)
    qty_label = 'Ordered' if doc.kind == 'po' else 'Received'
    cost_label = 'Order Cost' if doc.kind == 'po' else 'Cost'
    col_widths = [w * width for w in (0.28, 0.385, 0.10, 0.115, 0.12)]
    table_data = [[p('Item Name', head), p('Item Description', head), p(qty_label, head_right), p(cost_label, head_right),
                   p('Ext Cost', head_right)]]
    for row in data['rows']:
        table_data.append([p(row['name'], name_style), p(row['description']), p(qty(row['quantity']), right),
                           p(dollars(row['cost']), right), p(dollars(row['amount']), right)])
    if not data['rows']:
        table_data.append([p('No items.'), '', '', '', ''])
    table_data.append([p(f'Totals ({len(data["rows"])} line{"s" if len(data["rows"]) != 1 else ""})', bold), '',
                       p(qty(data['total_qty']), bold_right), '', p(dollars(data['total_cost']), bold_right)])
    style = [('BACKGROUND', (0, 0), (-1, 0), brand), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
             ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#F2F6F3')]),
             ('LINEBELOW', (0, 1), (-1, -3), 0.25, colors.HexColor('#D5DCD7')),
             ('LINEABOVE', (0, -1), (-1, -1), 1.2, brand), ('SPAN', (0, -1), (1, -1)),
             ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]
    lines_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    lines_table.setStyle(TableStyle(style))
    story.append(lines_table)

    if data['freight']:
        extra = Table([[p('Freight', bold_right), p(dollars(data['freight']), bold_right)],
                       [p('Total', bold_right), p(dollars(data['grand_total']), bold_right)]],
                      colWidths=[width - col_widths[-1], col_widths[-1]])
        story.append(extra)

    if data['footer']:
        notes = Table([[p(label, bold), p(value)] for label, value in data['footer']], colWidths=[1.1 * inch, width - 1.1 * inch])
        notes.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                   ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]))
        story += [Spacer(1, 12), KeepTogether(notes)]
    story += [Spacer(1, 8), p(f'Printed {timezone.localtime():%-m/%-d/%Y %-I:%M %p}', ParagraphStyle('printed', parent=small, textColor=colors.grey))]
    pdf.build(story, canvasmaker=NumberedCanvas)
    return stream.getvalue()
