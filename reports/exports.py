# SPDX-License-Identifier: AGPL-3.0-or-later
"""Excel and PDF (Letter) renderings of a report. Each returns bytes.

The same column list drives both so the exports always agree with the screen. Lines sold below the QB regular
price are shown in red, explained by a footnote whose * is red.
"""
import io
from decimal import Decimal

from django.utils import timezone

from sales.models import money, plain
from sales.printing import quantity as qty_text
from shop_settings.config import report_heading
from xml.sax.saxutils import escape

COLUMNS = ['Date', 'Receipt #', 'Customer', 'Item #', 'Item Name', 'Description', 'Qty', 'Price', 'Ext Price']
MONEY = '$#,##0.00;[Red]-$#,##0.00'
QTY = '#,##0.###'
BRAND = '1B4B34'
RED = 'C00000'
DISCOUNT_NOTE = '* Sold at Discounted Price'


def _cells(row):
    return [row.date, row.receipt, row.customer, row.sku, row.name, row.description, row.quantity, row.price, row.amount]


def _printed():
    return f"Printed {timezone.localtime():%-m/%-d/%Y %-I:%M %p}"


def xlsx_bytes(report):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = report.title[:31]
    bold = Font(bold=True)
    red = Font(color=RED)
    head_fill = PatternFill('solid', fgColor=BRAND)
    head_font = Font(bold=True, color='FFFFFF')
    band = PatternFill('solid', fgColor='E7F0E9')

    ws.append([report_heading()])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([report.title])
    ws['A2'].font = Font(bold=True, size=12)
    ws.append([report.subtitle])
    ws.append([_printed()])
    ws.append([])
    ws.append(COLUMNS)
    header_row = ws.max_row
    for c in range(1, len(COLUMNS) + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.fill, cell.font = head_fill, head_font
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    for g in report.groups:
        ws.append([g.vendor])
        for c in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=ws.max_row, column=c)
            cell.fill, cell.font = band, bold
        for r in g.rows:
            ws.append(_cells(r))
            row = ws.max_row
            ws.cell(row=row, column=1).number_format = 'm/d/yyyy'
            ws.cell(row=row, column=7).number_format = QTY
            ws.cell(row=row, column=8).number_format = MONEY
            ws.cell(row=row, column=9).number_format = MONEY
            if r.discounted:
                for c in range(1, len(COLUMNS) + 1):
                    ws.cell(row=row, column=c).font = red
        ws.append([f'Total {g.vendor}', '', '', '', '', '', g.quantity, '', g.amount])
        row = ws.max_row
        for c in (1, 7, 9):
            ws.cell(row=row, column=c).font = bold
        ws.cell(row=row, column=7).number_format = QTY
        ws.cell(row=row, column=9).number_format = MONEY
    ws.append([])
    ws.append(['Grand total', '', '', '', '', '', report.quantity, '', report.amount])
    row = ws.max_row
    for c in (1, 7, 9):
        ws.cell(row=row, column=c).font = bold
    ws.cell(row=row, column=7).number_format = QTY
    ws.cell(row=row, column=9).number_format = MONEY
    ws.append([f'{report.receipts} receipts · {report.lines} lines'])
    if report.discounted_lines:
        from openpyxl.cell.rich_text import CellRichText, TextBlock
        from openpyxl.cell.text import InlineFont
        ws.append([CellRichText(TextBlock(InlineFont(b=True, color=RED), '*'), TextBlock(InlineFont(b=True), DISCOUNT_NOTE[1:]))])

    widths = [11, 10, 26, 22, 30, 48, 8, 12, 13]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in (7, 8, 9):
        for row in ws.iter_rows(min_row=header_row, min_col=c, max_col=c):
            row[0].alignment = Alignment(horizontal='right')
    ws.print_title_rows = f'{header_row}:{header_row}'
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def pdf_bytes(report):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'], fontSize=8, leading=9.5)
    right = ParagraphStyle('right', parent=body, alignment=TA_RIGHT)
    head = ParagraphStyle('head', parent=body, textColor=colors.white, fontName='Helvetica-Bold')
    brand = colors.HexColor('#' + BRAND)

    def p(text, style=body):
        text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
        return Paragraph(text, style)

    def dollars(v):
        return f'${v:,.2f}' if v >= 0 else f'-${-v:,.2f}'

    def price(v):
        return dollars(money(v))

    red = colors.HexColor('#' + RED)
    red_body = ParagraphStyle('red_body', parent=body, textColor=red)
    red_right = ParagraphStyle('red_right', parent=right, textColor=red)

    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f'{report.title} — {report.subtitle}',
                            author=report_heading())
    width = doc.width
    col_widths = [w * width for w in (0.075, 0.06, 0.13, 0.12, 0.15, 0.23, 0.045, 0.09, 0.10)]

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.drawString(doc.leftMargin, 0.35 * inch, f'{report_heading()} · {report.title} · {report.subtitle}')
        canvas.drawRightString(doc.leftMargin + width, 0.35 * inch, f'Page {document.page}')
        canvas.restoreState()

    story = [Paragraph(escape(report_heading()), styles['Title']),
             Paragraph(report.title, styles['Heading2']),
             Paragraph(f'{report.subtitle} &nbsp;·&nbsp; {_printed()}', body),
             Spacer(1, 8)]

    bold = ParagraphStyle('bold', parent=body, fontName='Helvetica-Bold')
    bold_right = ParagraphStyle('bold_right', parent=right, fontName='Helvetica-Bold')
    grand_big = ParagraphStyle('grand', parent=bold, fontSize=8.5)
    grand_big_right = ParagraphStyle('grand_right', parent=bold_right, fontSize=8.5)
    band = colors.HexColor('#E7F0E9')
    header = [p(c, head) for c in COLUMNS]
    base_style = [('BACKGROUND', (0, 0), (-1, 0), brand), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                  ('FONTSIZE', (0, 0), (-1, -1), 8), ('LEADING', (0, 0), (-1, -1), 9.5), ('ALIGN', (6, 1), (-1, -1), 'RIGHT'),
                  ('LINEBELOW', (0, 0), (-1, 0), 0.75, brand), ('TOPPADDING', (0, 0), (-1, -1), 2),
                  ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]

    # Flatten the report into (cells, row-style) pairs, then lay it out in Tables of at most CHUNK rows:
    # reportlab's table layout cost grows faster than linearly, and 400-row tables keep a year of sales under
    # a few seconds. Each Table repeats the column header at every page top.
    CHUNK = 400
    flat = []
    for g in report.groups:
        flat.append(([p(g.vendor, bold)] + [''] * 8, [('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), band)]))
        for r in g.rows:
            if r.discounted:
                flat.append(([p(f'{r.date:%-m/%-d/%Y}', red_body), p(r.receipt, red_body), p(r.customer, red_body), p(r.sku, red_body),
                              p(r.name, red_body), p(r.description, red_body), p(qty_text(r.quantity), red_right),
                              p(price(r.price), red_right), p(dollars(r.amount), red_right)], []))
            else:
                flat.append(([f'{r.date:%-m/%-d/%Y}', r.receipt, p(r.customer), p(r.sku), p(r.name), p(r.description),
                              qty_text(r.quantity), price(r.price), dollars(r.amount)], []))
        flat.append(([p(f'Total {g.vendor}', bold), '', '', '', '', '', p(qty_text(g.quantity), bold_right), '',
                      p(dollars(g.amount), bold_right)],
                     [('SPAN', (0, 0), (5, 0)), ('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.grey), ('BOTTOMPADDING', (0, 0), (-1, 0), 6)]))
    if report.groups:
        flat.append(([p('Grand total', grand_big), '', '', '', '', '', p(qty_text(report.quantity), grand_big_right), '',
                      p(dollars(report.amount), grand_big_right)],
                     [('SPAN', (0, 0), (5, 0)), ('LINEABOVE', (0, 0), (-1, 0), 1.2, brand), ('TOPPADDING', (0, 0), (-1, 0), 4)]))
    else:
        flat.append(([p('No item sales in this range.')] + [''] * 8, [('SPAN', (0, 0), (-1, 0))]))

    def shifted(commands, row):
        return [(cmd[0], (cmd[1][0], row), (cmd[2][0], row)) + tuple(cmd[3:]) for cmd in commands]

    for offset in range(0, len(flat), CHUNK):
        part = flat[offset:offset + CHUNK]
        data = [header] + [cells for cells, _ in part]
        style = list(base_style)
        for i, (_, commands) in enumerate(part, start=1):
            style += shifted(commands, i)
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle(style))
        story.append(table)
    story += [Spacer(1, 6), p(f'{report.receipts} receipts · {report.lines} lines')]
    if report.discounted_lines:
        story.append(Paragraph(f'<font color="#{RED}">*</font> {DISCOUNT_NOTE[2:]}', ParagraphStyle('note', parent=body, fontName='Helvetica-Bold')))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
