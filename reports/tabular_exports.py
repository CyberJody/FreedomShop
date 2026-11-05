# SPDX-License-Identifier: AGPL-3.0-or-later
"""Column-driven Excel and PDF (Letter landscape) renderers for reports built from a list of Column specs.

Expects a report with: title, subtitle, columns (Column objects with key/label/kind/weight/total), groups (label,
rows as dicts, totals dict), totals dict, receipts, lines, discounted_lines, grouped. Rows may carry
'discounted': True to be drawn in red; a red-asterisk footnote is added when any are.
"""
import io

from django.utils import timezone

from sales.printing import quantity as qty_text

from .exports import BRAND, DISCOUNT_NOTE, MONEY, QTY, RED
from shop_settings.config import report_heading
from xml.sax.saxutils import escape


def _printed():
    return f"Printed {timezone.localtime():%-m/%-d/%Y %-I:%M %p}"


def _notes(report):
    notes = getattr(report, 'notes', None)
    return list(notes) if notes is not None else [f'{report.receipts} receipts · {report.lines} lines']


def _total_cells(report, label, totals):
    """One cell per column: label in the first column, totals under total columns, blanks elsewhere."""
    cells = []
    for i, c in enumerate(report.columns):
        if i == 0:
            cells.append(label)
        elif c.total:
            cells.append(totals[c.key])
        else:
            cells.append('')
    return cells


def xlsx_bytes(report):
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
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
    ncol = len(report.columns)
    formats = {'date': 'm/d/yyyy', 'qty': QTY, 'money': MONEY, 'percent': '0.00"%"'}

    def style_row(row, font=None):
        for i, c in enumerate(report.columns, start=1):
            cell = ws.cell(row=row, column=i)
            if c.kind in formats:
                cell.number_format = formats[c.kind]
            if c.kind in ('qty', 'money', 'int', 'percent'):
                cell.alignment = Alignment(horizontal='right')
            if font is not None:
                cell.font = font

    ws.append([report_heading()])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([report.title])
    ws['A2'].font = Font(bold=True, size=12)
    ws.append([report.subtitle])
    ws.append([_printed()])
    ws.append([])
    ws.append([c.label for c in report.columns])
    header_row = ws.max_row
    for i in range(1, ncol + 1):
        cell = ws.cell(row=header_row, column=i)
        cell.fill, cell.font = head_fill, head_font
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    for g in report.groups:
        if report.grouped:
            ws.append([g.label])
            for i in range(1, ncol + 1):
                cell = ws.cell(row=ws.max_row, column=i)
                cell.fill, cell.font = band, bold
        for row in g.rows:
            ws.append([row[c.key] for c in report.columns])
            style_row(ws.max_row, red if row.get('discounted') else None)
        if report.grouped:
            ws.append(_total_cells(report, f'Total {g.label}', g.totals))
            style_row(ws.max_row, bold)
    ws.append([])
    ws.append(_total_cells(report, 'Grand total', report.totals))
    style_row(ws.max_row, bold)
    for note in _notes(report):
        ws.append([note])
    if report.discounted_lines:
        ws.append([CellRichText(TextBlock(InlineFont(b=True, color=RED), '*'), TextBlock(InlineFont(b=True), DISCOUNT_NOTE[1:]))])

    widths = {'date': 11, 'text': 24, 'qty': 8, 'money': 13, 'int': 8, 'percent': 10}
    for i, c in enumerate(report.columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 48 if c.key == 'description' else widths[c.kind]
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
    red = colors.HexColor('#' + RED)
    red_body = ParagraphStyle('red_body', parent=body, textColor=red)
    red_right = ParagraphStyle('red_right', parent=right, textColor=red)
    brand = colors.HexColor('#' + BRAND)
    band = colors.HexColor('#E7F0E9')

    def p(text, style=body):
        text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
        return Paragraph(text, style)

    def dollars(v):
        return f'${v:,.2f}' if v >= 0 else f'-${-v:,.2f}'

    def fmt(c, value):
        if c.kind == 'date':
            return f'{value:%-m/%-d/%Y}'
        if c.kind == 'qty':
            return qty_text(value)
        if c.kind == 'money':
            return dollars(value)
        if c.kind == 'percent':
            return '' if value is None else f'{value:.2f}%'
        return str(value)

    numeric = {'qty', 'money', 'int', 'percent'}
    first_numeric = next((i for i, c in enumerate(report.columns) if c.kind in numeric), len(report.columns))

    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f'{report.title} — {report.subtitle}', author=report_heading())
    width = doc.width
    total_weight = sum(c.weight for c in report.columns)
    col_widths = [c.weight / total_weight * width for c in report.columns]

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.drawString(doc.leftMargin, 0.35 * inch, f'{report_heading()} · {report.title} · {report.subtitle}')
        canvas.drawRightString(doc.leftMargin + width, 0.35 * inch, f'Page {document.page}')
        canvas.restoreState()

    story = [Paragraph(escape(report_heading()), styles['Title']), Paragraph(report.title, styles['Heading2']),
             Paragraph(f'{report.subtitle} &nbsp;·&nbsp; {_printed()}', body), Spacer(1, 8)]
    header = [p(c.label, head) for c in report.columns]
    base = [('BACKGROUND', (0, 0), (-1, 0), brand), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('LINEBELOW', (0, 0), (-1, 0), 0.75, brand), ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]
    if first_numeric < len(report.columns):
        base.append(('ALIGN', (first_numeric, 1), (-1, -1), 'RIGHT'))

    def total_row(label, totals, label_style, num_style, extra):
        cells = []
        for i, c in enumerate(report.columns):
            if i == 0:
                cells.append(p(label, label_style))
            elif c.total:
                cells.append(p(fmt(c, totals[c.key]), num_style))
            else:
                cells.append('')
        span_end = max(first_numeric - 1, 0)
        commands = [('SPAN', (0, 0), (span_end, 0))] if span_end > 0 else []
        return cells, commands + extra

    flat = []
    for g in report.groups:
        if report.grouped:
            flat.append(([p(g.label, bold)] + [''] * (len(report.columns) - 1), [('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), band)]))
        for row in g.rows:
            if row.get('discounted'):
                cells = [p(fmt(c, row[c.key]), red_right if c.kind in numeric else red_body) for c in report.columns]
            else:
                cells = [fmt(c, row[c.key]) if c.kind in numeric or c.kind == 'date' or c.key == 'receipt' else p(fmt(c, row[c.key])) for c in report.columns]
            flat.append((cells, []))
        if report.grouped:
            flat.append(total_row(f'Total {g.label}', g.totals, bold, bold_right, [('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.grey), ('BOTTOMPADDING', (0, 0), (-1, 0), 6)]))
    if report.lines:
        flat.append(total_row('Grand total', report.totals, ParagraphStyle('g', parent=bold, fontSize=8.5),
                              ParagraphStyle('gr', parent=bold_right, fontSize=8.5), [('LINEABOVE', (0, 0), (-1, 0), 1.2, brand), ('TOPPADDING', (0, 0), (-1, 0), 4)]))
    else:
        flat.append(([p(getattr(report, 'empty_message', 'No sales in this period.'))] + [''] * (len(report.columns) - 1), [('SPAN', (0, 0), (-1, 0))]))

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
    if report.discounted_lines:
        story.append(Paragraph(f'<font color="#{RED}">*</font> {DISCOUNT_NOTE[2:]}', bold))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
