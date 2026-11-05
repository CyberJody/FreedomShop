# SPDX-License-Identifier: AGPL-3.0-or-later
"""Excel and PDF (Letter) renderings of the Inventory On Hand report."""
import io

from django.utils import timezone

from .exports import BRAND, MONEY, QTY
from sales.printing import quantity as qty_text
from shop_settings.config import report_heading
from xml.sax.saxutils import escape

COLUMNS = ['Item #', 'Item Name', 'Description', 'Vendor', 'On Hand', 'Avg Cost', 'Ext Cost']


def _printed():
    return f"Printed {timezone.localtime():%-m/%-d/%Y %-I:%M %p}"


def _notes(report):
    notes = [f'{report.items} items · {qty_text(report.on_hand)} units on hand · inventory items only, current quantities.']
    if report.negative_items:
        notes.append(f'{report.negative_items} item{"s" if report.negative_items != 1 else ""} show a negative on-hand quantity (sold beyond recorded stock); they reduce the total.')
    if report.skipped_zero:
        notes.append(f'{report.skipped_zero} item{"s" if report.skipped_zero != 1 else ""} with zero on hand not shown.')
    if report.trashed_items:
        notes.append(f'{report.trashed_items} item{"s" if report.trashed_items != 1 else ""} marked "(in Items Trash)" hold stock received or returned while in the Items Trash; they count in the total.')
    return notes


def xlsx_bytes(report):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = 'Inventory On Hand'
    bold = Font(bold=True)
    head_fill = PatternFill('solid', fgColor=BRAND)
    head_font = Font(bold=True, color='FFFFFF')
    band = PatternFill('solid', fgColor='E7F0E9')

    ws.append([report_heading()])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([report.title])
    ws['A2'].font = Font(bold=True, size=12)
    ws.append([f'{report.subtitle} · as of {timezone.localtime():%-m/%-d/%Y}'])
    ws.append([_printed()])
    ws.append([])
    ws.append(COLUMNS)
    header_row = ws.max_row
    for c in range(1, len(COLUMNS) + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.fill, cell.font = head_fill, head_font
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    def formats(row):
        ws.cell(row=row, column=5).number_format = QTY
        ws.cell(row=row, column=6).number_format = MONEY
        ws.cell(row=row, column=7).number_format = MONEY
        for c in (5, 6, 7):
            ws.cell(row=row, column=c).alignment = Alignment(horizontal='right')

    for g in report.groups:
        ws.append([g.department])
        for c in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=ws.max_row, column=c)
            cell.fill, cell.font = band, bold
        for r in g.rows:
            ws.append([r.sku, r.name, r.description, r.vendor, r.on_hand, r.cost, r.value])
            formats(ws.max_row)
        ws.append([f'Total {g.department}', f'{g.items} items', '', '', g.on_hand, '', g.value])
        formats(ws.max_row)
        for c in (1, 2, 5, 7):
            ws.cell(row=ws.max_row, column=c).font = bold
    ws.append([])
    ws.append(['Grand total', f'{report.items} items', '', '', report.on_hand, '', report.value])
    formats(ws.max_row)
    for c in (1, 2, 5, 7):
        ws.cell(row=ws.max_row, column=c).font = bold
    for note in _notes(report):
        ws.append([note])

    for i, w in enumerate([24, 30, 50, 26, 10, 12, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
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
    bold = ParagraphStyle('bold', parent=body, fontName='Helvetica-Bold')
    bold_right = ParagraphStyle('bold_right', parent=right, fontName='Helvetica-Bold')
    head = ParagraphStyle('head', parent=bold, textColor=colors.white)
    brand = colors.HexColor('#' + BRAND)
    band = colors.HexColor('#E7F0E9')

    def p(text, style=body):
        text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
        return Paragraph(text, style)

    def dollars(v):
        return f'${v:,.2f}' if v >= 0 else f'-${-v:,.2f}'

    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f'{report.title} — {report.subtitle}', author=report_heading())
    width = doc.width
    col_widths = [w * width for w in (0.16, 0.20, 0.30, 0.14, 0.06, 0.07, 0.07)]

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.drawString(doc.leftMargin, 0.35 * inch, f'{report_heading()} · {report.title} · {report.subtitle} · as of {timezone.localtime():%-m/%-d/%Y}')
        canvas.drawRightString(doc.leftMargin + width, 0.35 * inch, f'Page {document.page}')
        canvas.restoreState()

    story = [Paragraph(escape(report_heading()), styles['Title']), Paragraph(report.title, styles['Heading2']),
             Paragraph(f'{report.subtitle} &nbsp;·&nbsp; as of {timezone.localtime():%-m/%-d/%Y} &nbsp;·&nbsp; {_printed()}', body), Spacer(1, 8)]
    header = [p(c, head) for c in COLUMNS]
    base = [('BACKGROUND', (0, 0), (-1, 0), brand), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (4, 1), (-1, -1), 'RIGHT'), ('LINEBELOW', (0, 0), (-1, 0), 0.75, brand), ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]
    flat = []
    for g in report.groups:
        flat.append(([p(g.department, bold)] + [''] * 6, [('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), band)]))
        for r in g.rows:
            flat.append(([p(r.sku), p(r.name), p(r.description), p(r.vendor), qty_text(r.on_hand), dollars(r.cost), dollars(r.value)], []))
        flat.append(([p(f'Total {g.department}', bold), p(f'{g.items} items', bold), '', '', p(qty_text(g.on_hand), bold_right), '', p(dollars(g.value), bold_right)],
                     [('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.grey), ('BOTTOMPADDING', (0, 0), (-1, 0), 6)]))
    flat.append(([p('Grand total', ParagraphStyle('g', parent=bold, fontSize=8.5)), p(f'{report.items} items', bold), '', '',
                  p(qty_text(report.on_hand), bold_right), '', p(dollars(report.value), ParagraphStyle('gr', parent=bold_right, fontSize=8.5))],
                 [('LINEABOVE', (0, 0), (-1, 0), 1.2, brand), ('TOPPADDING', (0, 0), (-1, 0), 4)]))
    CHUNK = 400
    for offset in range(0, len(flat), CHUNK):
        part = flat[offset:offset + CHUNK]
        data = [header] + [cells for cells, _ in part]
        style = list(base)
        for i, (_, commands) in enumerate(part, start=1):
            style += [(cmd[0], (cmd[1][0], i), (cmd[2][0], i)) + tuple(cmd[3:]) for cmd in commands]
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle(style))
        story.append(table)
    story.append(Spacer(1, 6))
    story += [p(note) for note in _notes(report)]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
