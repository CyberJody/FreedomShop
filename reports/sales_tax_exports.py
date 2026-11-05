# SPDX-License-Identifier: AGPL-3.0-or-later
"""Excel and PDF (Letter) renderings of the Sales Tax report."""
import io

from django.utils import timezone

from .exports import BRAND, MONEY
from shop_settings.config import tax as shop_tax
from shop_settings.config import report_heading
from xml.sax.saxutils import escape

COLUMNS = ['Date', 'Receipt #', 'Customer', 'Type', 'Gross Sales', 'Discount', 'Non-taxable', 'Taxable Sales', 'Sales Tax', 'Total']
SUMMARY_COLUMNS = ['Location', 'Receipts', 'Gross Sales', 'Discount', 'Non-taxable', 'Taxable Sales', 'Sales Tax', 'Total']
MONEY_COLS_DETAIL = range(5, 11)
MONEY_COLS_SUMMARY = range(3, 9)


def _printed():
    return f"Printed {timezone.localtime():%-m/%-d/%Y %-I:%M %p}"


def _summary_rows(report):
    """(values, level): level 0 = location or grand total (bold), 1 = Sales receipts / Customer order payments."""
    rows = []
    for s in report.sections:
        rows.append(([s.title, s.receipts, s.gross, s.discount, s.nontaxable, s.taxable, s.tax, s.total], 0))
        rows += [(['    ' + p.label, p.receipts, p.gross, p.discount, p.nontaxable, p.taxable, p.tax, p.total], 1) for p in s.part_list]
    rows.append((['All locations', report.receipts, report.gross, report.discount, report.nontaxable, report.taxable, report.tax, report.total], 0))
    return rows


def _money(v):
    return f'${v:,.2f}' if v >= 0 else f'-${-v:,.2f}'


def _notes(report):
    if getattr(report, 'daily', False):
        return report.notes
    notes = ['Money received in the period, on the day it was received (the store’s ledger basis; same ledger as Daily Totals): '
             'sales receipts for the part not charged to account, customer order payments (payment × order tax ÷ order total, '
             'adding up to exactly the order’s tax), and payments on account (tax released from the charges they pay, oldest first). '
             'Reversals and refunds carry negative amounts.',
             f'Charged to account this period, tax deferred: {_money(report.charged_to_account_tax)} (on {_money(report.charged_to_account)}) · '
             f'Payments on account this period, tax: {_money(report.payments_on_account_tax)} (on {_money(report.payments_on_account)}) · '
             f'Estimated (no charge in the data: history before the backup, or a prepayment held for the customer’s next charge): '
             f'{_money(report.estimated_tax)} (on {_money(report.estimated_total)}, '
             f'{report.estimated_count} payment{"s" if report.estimated_count != 1 else ""}, {shop_tax().estimate_note}).']
    if report.reconciled_tax:
        notes.append(f'Credits met the charges they funded this period (prepayments taxed at the {shop_tax().estimate_fraction} estimate when received, returns '
                     f'credited to the account): trued up to the charges’ own tax, location and taxable / non-taxable amounts by '
                     f'{_money(report.reconciled_tax)} ("tax trued up" rows; no money received, their Totals net to zero).')
    notes.append('A payment on account is split by the charges it pays (oldest first) and each part is listed under that charge’s '
                 'location with the charge’s own taxable / non-taxable amounts; a payment covering taxable and exempt charges '
                 'appears in both sections.')
    if report.gift_total:
        notes.append(f'Gift certificates redeemed: {_money(report.gift_total)} — not money received (it came in when the certificates were sold); '
                     f'the tax on that merchandise, {_money(report.gift_tax)}, is included above on the redemption day.')
    if report.missing_tenders:
        notes.append(f'{report.missing_tenders} receipt{"s" if report.missing_tenders != 1 else ""} with missing tender information; unknown amounts are not invented.')
    if report.derived_tenders:
        notes.append(f'{report.derived_tenders} receipt{"s" if report.derived_tenders != 1 else ""} with one tender amount the backup could not decode, '
                     f'taken as the receipt total less the other tenders.')
    if report.unlinked_deposits:
        notes.append(f'Not included above: {report.unlinked_deposits} deposit receipt{"s" if report.unlinked_deposits != 1 else ""} '
                     f'totaling {_money(report.unlinked_total)} with no customer order in the QuickBooks data.')
    if report.applied_deposits:
        notes.append(f'Not included above: {report.applied_deposits} deposit{"s" if report.applied_deposits != 1 else ""} totaling '
                     f'{_money(report.applied_total)} on orders later picked up on a sales receipt (taxed on that receipt).')
    if report.native_pickups:
        notes.append(f'Not included above: {report.native_pickups} pickup receipt{"s" if report.native_pickups != 1 else ""} paid on a sales order '
                     f'(tax counted with the order payments).')
    if report.deposits_count:
        notes.append(f'Not included above: {report.deposits_count} payout receipt{"s" if report.deposits_count != 1 else ""} '
                     f'totaling {_money(report.deposits_total)}.')
    if report.other_locations:
        notes.append(f'{report.other_locations} receipt(s) with an unrecognized tax location were placed by their exempt flag.')
    return notes


def xlsx_bytes(report):
    if getattr(report, 'daily', False):
        from . import daily_totals_exports
        return daily_totals_exports.xlsx_bytes(report)
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = 'Sales Tax'
    bold = Font(bold=True)
    head_fill = PatternFill('solid', fgColor=BRAND)
    head_font = Font(bold=True, color='FFFFFF')
    band = PatternFill('solid', fgColor='E7F0E9')

    def header(columns):
        ws.append(columns)
        row = ws.max_row
        for c in range(1, len(columns) + 1):
            cell = ws.cell(row=row, column=c)
            cell.fill, cell.font = head_fill, head_font
        return row

    def money_formats(row, cols):
        for c in cols:
            ws.cell(row=row, column=c).number_format = MONEY
            ws.cell(row=row, column=c).alignment = Alignment(horizontal='right')

    ws.append([report_heading()])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f'{report.title} {"Detail" if report.detailed else "Summary"}'])
    ws['A2'].font = Font(bold=True, size=12)
    ws.append([report.period.label])
    ws.append([_printed()])
    ws.append([])

    ws.append(['Summary'])
    ws.cell(row=ws.max_row, column=1).font = bold
    header(SUMMARY_COLUMNS)
    for values, level in _summary_rows(report):
        ws.append(values)
        money_formats(ws.max_row, MONEY_COLS_SUMMARY)
        if level == 0:
            for c in range(1, len(SUMMARY_COLUMNS) + 1):
                ws.cell(row=ws.max_row, column=c).font = bold

    if report.detailed:
        for section in report.sections:
            ws.append([])
            ws.append([section.title])
            for c in range(1, len(COLUMNS) + 1):
                cell = ws.cell(row=ws.max_row, column=c)
                cell.fill, cell.font = band, bold
            first = header(COLUMNS)
            for r in section.rows:
                ws.append([r.date, r.receipt, r.customer, r.type_label, r.gross, r.discount, r.nontaxable, r.taxable, r.tax, r.total])
                ws.cell(row=ws.max_row, column=1).number_format = 'm/d/yyyy'
                money_formats(ws.max_row, MONEY_COLS_DETAIL)
            ws.append([f'Total {section.title}', section.receipts, '', '', section.gross, section.discount, section.nontaxable, section.taxable, section.tax, section.total])
            money_formats(ws.max_row, MONEY_COLS_DETAIL)
            for c in (1, 2, *MONEY_COLS_DETAIL):
                ws.cell(row=ws.max_row, column=c).font = bold
            if not section.rows:
                ws.append(['No receipts in this period.'])

    ws.append([])
    for note in _notes(report):
        ws.append([note])
    widths = [30, 11, 26, 10, 14, 12, 14, 15, 13, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def pdf_bytes(report):
    if getattr(report, 'daily', False):
        from . import daily_totals_exports
        return daily_totals_exports.pdf_bytes(report)
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'], fontSize=8, leading=9.5)
    right = ParagraphStyle('right', parent=body, alignment=TA_RIGHT)
    bold = ParagraphStyle('bold', parent=body, fontName='Helvetica-Bold')
    bold_right = ParagraphStyle('bold_right', parent=right, fontName='Helvetica-Bold')
    head = ParagraphStyle('head', parent=bold, textColor=colors.white)
    brand = colors.HexColor('#' + BRAND)
    band = colors.HexColor('#E7F0E9')

    def p(text, style=body):
        text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return Paragraph(text, style)

    def dollars(v):
        return f'${v:,.2f}' if v >= 0 else f'-${-v:,.2f}'

    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f'{report.title} — {report.subtitle}',
                            author=report_heading())
    width = doc.width

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.drawString(doc.leftMargin, 0.35 * inch, f'{report_heading()} · {report.title} · {report.subtitle}')
        canvas.drawRightString(doc.leftMargin + width, 0.35 * inch, f'Page {document.page}')
        canvas.restoreState()

    base = [('BACKGROUND', (0, 0), (-1, 0), brand), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (-1, 0), 0.75, brand),
            ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]

    story = [Paragraph(escape(report_heading()), styles['Title']),
             Paragraph(f'{report.title} {"Detail" if report.detailed else "Summary"}', styles['Heading2']),
             Paragraph(f'{report.period.label} &nbsp;·&nbsp; {_printed()}', body), Spacer(1, 8)]

    # Summary table
    summary = [[p(c, head) for c in SUMMARY_COLUMNS]]
    sub = ParagraphStyle('sub', parent=body, leftIndent=12, textColor=colors.HexColor('#485365'))
    for values, level in _summary_rows(report):
        st, str_ = (bold, bold_right) if level == 0 else (sub, right)
        summary.append([p(values[0].strip(), st), p(values[1], str_)] + [p(dollars(v), str_) for v in values[2:]])
    t = Table(summary, colWidths=[w * width for w in (0.30, 0.08, 0.105, 0.095, 0.105, 0.105, 0.105, 0.105)], repeatRows=1)
    t.setStyle(TableStyle(base + [('LINEABOVE', (0, -1), (-1, -1), 1.0, brand)]))
    story += [t, Spacer(1, 10)]

    if report.detailed:
        col_widths = [w * width for w in (0.075, 0.07, 0.16, 0.14, 0.095, 0.075, 0.095, 0.095, 0.09, 0.105)]
        CHUNK = 400
        for section in report.sections:
            flat = [([p(section.title, bold)] + [''] * 9, [('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), band)])]
            for r in section.rows:
                flat.append(([f'{r.date:%-m/%-d/%Y}', r.receipt, p(r.customer), p(r.type_label), dollars(r.gross), dollars(r.discount),
                              dollars(r.nontaxable), dollars(r.taxable), dollars(r.tax), dollars(r.total)], []))
            if not section.rows:
                flat.append(([p('No receipts in this period.')] + [''] * 9, [('SPAN', (0, 0), (-1, 0))]))
            flat.append(([p(f'Total {section.title}', bold), p(str(section.receipts), bold), '', ''] +
                         [p(dollars(v), bold_right) for v in (section.gross, section.discount, section.nontaxable, section.taxable, section.tax, section.total)],
                         [('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.grey), ('BOTTOMPADDING', (0, 0), (-1, 0), 6)]))
            header_row = [p(c, head) for c in COLUMNS]
            for offset in range(0, len(flat), CHUNK):
                part = flat[offset:offset + CHUNK]
                data = [header_row] + [cells for cells, _ in part]
                style = base + [('FONTSIZE', (0, 0), (-1, -1), 8), ('ALIGN', (4, 1), (-1, -1), 'RIGHT')]
                for i, (_, commands) in enumerate(part, start=1):
                    style += [(cmd[0], (cmd[1][0], i), (cmd[2][0], i)) + tuple(cmd[3:]) for cmd in commands]
                table = Table(data, colWidths=col_widths, repeatRows=1)
                table.setStyle(TableStyle(style))
                story.append(table)
            story.append(Spacer(1, 6))

    story.append(KeepTogether([p(note) for note in _notes(report)]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
