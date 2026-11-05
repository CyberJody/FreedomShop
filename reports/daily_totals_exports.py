# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daily Totals exports: by-day overview followed by dated document groups."""
import io
from copy import copy

from . import tabular_exports
from .exports import BRAND, MONEY
from shop_settings.config import report_heading


def xlsx_bytes(report):
    from openpyxl import load_workbook
    # The generic Excel writer already preserves blank money cells.
    wb = load_workbook(io.BytesIO(tabular_exports.xlsx_bytes(report.overview)))
    wb.active.title = 'By day'
    sheet = wb.active
    sheet.append([])
    sheet.append(['Sales / Returns / Net', '', '', '', 'Amount'])
    sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=4)
    for row in report.summary:
        sheet.append([row['label'], '', '', '', row['amount']])
        sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=4)
        sheet.cell(sheet.max_row, 5).number_format = MONEY

    detail = load_workbook(io.BytesIO(tabular_exports.xlsx_bytes(report))).active
    ws = wb.create_sheet('Documents')
    for row in detail:
        for cell in row:
            dest = ws.cell(cell.row, cell.column, cell.value)
            dest._style = copy(cell._style)
    for key, dim in detail.column_dimensions.items():
        ws.column_dimensions[key] = copy(dim)
    ws.freeze_panes = detail.freeze_panes
    ws.page_setup = copy(detail.page_setup)
    ws.sheet_properties = copy(detail.sheet_properties)
    ws.print_title_rows = detail.print_title_rows
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def pdf_bytes(report):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

    styles = getSampleStyleSheet()
    body = ParagraphStyle('daily', fontName='Helvetica', fontSize=6.5, leading=8)
    right = ParagraphStyle('daily-right', parent=body, alignment=TA_RIGHT)
    bold = ParagraphStyle('daily-bold', parent=body, fontName='Helvetica-Bold')
    head = ParagraphStyle('daily-head', parent=bold, textColor=colors.white)
    head_right = ParagraphStyle('daily-head-right', parent=head, alignment=TA_RIGHT)
    brand = colors.HexColor('#' + BRAND)

    def p(text, style=body):
        from xml.sax.saxutils import escape
        return Paragraph(escape(str(text)), style)

    def value(column, amount):
        if amount is None:
            return ''
        if column.kind == 'money':
            return f'-${-amount:,.2f}' if amount < 0 else f'${amount:,.2f}'
        if column.kind == 'date':
            return f'{amount:%-m/%-d/%Y}'
        return amount

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(letter), leftMargin=24, rightMargin=24,
                            topMargin=30, bottomMargin=30, title=report.title, author=report_heading())
    story = [p(report_heading(), styles['Title']), p(report.title, styles['Heading2']),
             p(report.subtitle + ' · ' + tabular_exports._printed()), Spacer(1, 8)]

    def table(section):
        columns = section.columns
        first = next(i for i, c in enumerate(columns) if c.total)
        flat = []

        def total(label, amounts):
            flat.append(([p(label, bold)] + [''] * (first - 1) +
                         [p(value(c, amounts[c.key]), right) for c in columns[first:]],
                         [('SPAN', (0, 0), (first - 1, 0)), ('LINEABOVE', (0, 0), (-1, 0), .5, brand)]))
        for group in section.groups:
            if section.grouped:
                flat.append(([p(group.label, bold)] + [''] * (len(columns) - 1),
                             [('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E7F0E9'))]))
            for row in group.rows:
                flat.append(([p(value(c, row[c.key]), right if c.total else body) for c in columns], []))
            if section.grouped:
                total('Total ' + group.label, group.totals)
        total('Grand total', section.totals)
        widths = [doc.width * c.weight / sum(c.weight for c in columns) for c in columns]
        for offset in range(0, len(flat), 300):
            chunk = flat[offset:offset + 300]
            data = [[p(c.label, head_right if c.kind == 'money' else head) for c in columns]] + [cells for cells, _ in chunk]
            commands = [('BACKGROUND', (0, 0), (-1, 0), brand), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]
            for index, (_, cmds) in enumerate(chunk, 1):
                commands += [(cmd[0], (cmd[1][0], index), (cmd[2][0], index)) + cmd[3:] for cmd in cmds]
            t = Table(data, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle(commands))
            story.append(t)

    table(report.overview)
    story.extend([Spacer(1, 14), p('Sales / Returns / Net', styles['Heading2'])])
    summary_table = Table([[p(row['label'], bold), p(f"-${-row['amount']:,.2f}" if row['amount'] < 0 else f"${row['amount']:,.2f}", right)] for row in report.summary], colWidths=[doc.width * .8, doc.width * .2])
    summary_table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'), ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.white,colors.HexColor('#E7F0E9')]), ('TOPPADDING',(0,0),(-1,-1),6), ('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    story.append(summary_table)
    story.extend([PageBreak(), p('Documents', styles['Heading2'])])
    table(report)
    story.append(Spacer(1, 8))
    story.extend(p(note) for note in report.notes)

    def footer(canvas, document):
        canvas.setFont('Helvetica', 7)
        canvas.drawString(24, 15, report.title + ' · ' + report.period.label)
        canvas.drawRightString(768, 15, f'Page {document.page}')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
