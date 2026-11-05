# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin scanner test: a Letter sheet of QR codes at several sizes and a scan log.

Matrices generated with Segno are committed in scanner_test_qr.json. Payloads are
made-up TEST codes at a documentation URL plus one bare code. No data is saved.
"""
import io
import json
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods, require_POST

from .admin_mode import admin_mode_required

INCH = 72
PAGE_W, PAGE_H = 8.5 * INCH, 11 * INCH
SL548_QR = 0.9  # inches: the QR on an SL548 piece label
# (payload, printed QR size in inches, caption). The sizes include the quiet zone, like a printed label.
SHEET = [
    ('https://example.com/?wc=FS-TEST-0001', 0.5, '½"'),
    ('FS-TEST-0002', 0.75, '¾" · bare code (no web address)'),
    ('https://example.com/?wc=FS-TEST-0003', SL548_QR, f'{SL548_QR}" · same as a PO item label (SL548)'),
    ('https://example.com/?wc=FS-TEST-0004', 1.0, '1"'),
    ('https://example.com/?wc=FS-TEST-0005', 1.5, '1½"'),
    ('https://example.com/?wc=FS-TEST-0006', 2.0, '2"'),
    ('https://example.com/?wc=FS-TEST-0007', 3.0, '3"'),
]
TRAY_1 = 'tray-1'
_MATRICES = None


def matrices():
    global _MATRICES
    if _MATRICES is None:
        _MATRICES = json.loads((Path(__file__).parent / 'scanner_test_qr.json').read_text())
    return _MATRICES


def svg(payload, size_px):
    """Inline SVG of one code (crisp at any size: one rect per dark module)."""
    m = matrices()[payload]
    n = m['size']
    rects = ''.join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for y, row in enumerate(m['rows'])
                    for x, bit in enumerate(row) if bit == '1')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{size_px}" height="{size_px}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR {payload}"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<g fill="#000">{rects}</g></svg>')


def draw_code(c, payload, x, y, size):
    """Draw one code with its top-left at (x, y from the top), ``size`` points square, one rect per dark module."""
    m = matrices()[payload]
    n = m['size']
    cell = size / n
    c.setFillColorRGB(1, 1, 1)
    c.rect(x, PAGE_H - y - size, size, size, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    for row, bits in enumerate(m['rows']):
        for col, bit in enumerate(bits):
            if bit == '1':
                c.rect(x + col * cell, PAGE_H - y - (row + 1) * cell, cell, cell, stroke=0, fill=1)


def sheet_pdf():
    """The Letter test sheet: every code with its size caption and payload, largest codes lower on the page."""
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))
    c.setTitle('Scanner Test sheet')
    c.setFont('Helvetica-Bold', 14)
    c.drawString(0.5 * INCH, PAGE_H - 0.6 * INCH, 'Scanner Test sheet — QR codes by size')
    c.setFont('Helvetica', 9)
    c.drawString(0.5 * INCH, PAGE_H - 0.85 * INCH, 'Fake TEST codes in the PO item label format. Scan each from farther away until it')
    c.drawString(0.5 * INCH, PAGE_H - 0.98 * INCH, 'stops reading; note the distance per size on the Scanner Test page.')
    # Small codes (up to 1") in one row, each in its own 1.85" column with the caption and payload under it; the
    # big ones below, one per row with the caption beside it.
    top = 1.3 * INCH
    small = [s for s in SHEET if s[1] <= 1.0]
    big = [s for s in SHEET if s[1] > 1.0]
    col_w = (PAGE_W - 1.0 * INCH) / len(small)
    row_h = max(s[1] for s in small) * INCH
    for i, (payload, inches, caption) in enumerate(small):
        size = inches * INCH
        x = 0.5 * INCH + i * col_w
        draw_code(c, payload, x, top + row_h - size, size)   # bottoms aligned
        c.setFont('Helvetica-Bold', 10)
        c.drawString(x, PAGE_H - top - row_h - 14, caption.split(' · ')[0])
        if ' · ' in caption:
            c.setFont('Helvetica', 8)
            c.drawString(x, PAGE_H - top - row_h - 26, caption.split(' · ', 1)[1])
        c.setFont('Helvetica', 6.5)
        c.drawString(x, PAGE_H - top - row_h - 37, payload.replace('https://', ''))
    y = top + row_h + 0.65 * INCH
    for payload, inches, caption in big:
        size = inches * INCH
        draw_code(c, payload, 0.5 * INCH, y, size)
        c.setFont('Helvetica-Bold', 12)
        c.drawString(0.5 * INCH + size + 0.3 * INCH, PAGE_H - y - 16, caption)
        c.setFont('Helvetica', 8)
        c.drawString(0.5 * INCH + size + 0.3 * INCH, PAGE_H - y - 30, payload)
        y += size + 0.3 * INCH
    c.showPage()
    c.save()
    return buffer.getvalue()


def default_printer(printers):
    """Prefer a color Letter printer using Tray 1 for the test sheet. The office printer is the color Letter row; prefer
    its Tray 1 row, else the color row itself (its default tray is Tray 1), else any Tray 1 row, else the first Letter."""
    rows = list(printers)
    color = [p for p in rows if p.color]
    hosts = {p.host for p in color}
    for pick in ([p for p in rows if p.media_source == TRAY_1 and p.host in hosts],
                 [p for p in color if p.media_source in ('', TRAY_1)], [p for p in rows if p.media_source == TRAY_1], rows):
        if pick:
            return pick[0]
    return None


@admin_mode_required
@require_http_methods(['GET', 'HEAD'])
def scanner_test_view(request):
    from reports import direct_print
    printers = list(direct_print.letter_printers())
    chosen = default_printer(printers)
    codes = [dict(payload=p, inches=i, caption=cap, svg=svg(p, round(i * 96))) for p, i, cap in SHEET]
    response = render(request, 'scanner-test.html', {'title': 'Scanner Test', 'codes': codes, 'printers': printers,
                                                     'printer': chosen})
    response['Cache-Control'] = 'no-store'
    return response


@admin_mode_required
@require_http_methods(['GET', 'HEAD'])
def scanner_test_pdf(request):
    response = HttpResponse(sheet_pdf(), content_type='application/pdf')
    response['Content-Disposition'] = 'inline; filename="scanner-test-sheet.pdf"'
    return response


@admin_mode_required
@require_POST
def scanner_test_print(request):
    """Direct Print of the sheet to the chosen Letter printer (default: office Brother, Tray 1)."""
    from reports import direct_print
    from sales.printing import PrintError
    printers = list(direct_print.letter_printers())
    key = request.POST.get('printer', '')
    printer = (next((p for p in printers if str(p.pk) == key), None) if key.isdigit() and len(key) <= 18 else None) or default_printer(printers)
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    try:
        message = direct_print.print_report(printer, sheet_pdf(), 'Scanner Test sheet')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse({'ok': True, 'message': message})
