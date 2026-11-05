# SPDX-License-Identifier: AGPL-3.0-or-later
"""PO item labels: one label per piece ordered, for the vendor to stick on each piece before it ships.

Sheet: SheetLabels SL548 (2.688" x 2", 3 across x 5 down = 15 per Letter sheet). Geometry from the vendor's own spec
page and PDF template (the import parity checks SL548-template.pdf):
top/bottom margin 0.25", left/right 0.15625", 0.0625" between columns, 0.125" between rows. The column pitch is
2.75", so the label is 2 11/16" (2.6875") wide; the vendor rounds that to 2.688". Labels fill left to right, top to
bottom; copies of the whole set follow on directly (no half-empty sheets between them).

Each label: a QR code (top left) holding the store website address with the piece code (piece_codes.piece_url), the
code printed under it as a read-aloud fallback, the item name large and bold, the PO number, "PO Quantity: N" and
"Piece K of N", and the line's description (wrapped, up to three lines). Text never shrinks below 12 pt (name) / 10 pt
(description); longer text is cut with "...".

The PDF is the print master: Direct Print rasterizes it for the Brother Letter printers (reports.direct_print), like POs.
"""
import io
import math
from decimal import Decimal

from django.db import transaction

from sales.printing import testing_marks
from .models import PieceLabel, PurchaseDocument
from .piece_codes import new_code, piece_url
from shop_settings.config import business

INCH = 72.0
PAGE_W, PAGE_H = 8.5 * INCH, 11 * INCH
LABEL_W, LABEL_H = 2.6875 * INCH, 2.0 * INCH
COLUMN_LEFT = (0.15625, 2.90625, 5.65625)        # inches from the sheet's left edge
ROW_TOP = (0.25, 2.375, 4.5, 6.625, 8.75)       # inches from the sheet's top edge
PER_SHEET = len(COLUMN_LEFT) * len(ROW_TOP)
CORNER = 0.0625 * INCH                           # outline corner radius (draft / alignment sheet only)

PAD = 0.08 * INCH
QR_SIZE = 1.12 * INCH          # including the quiet zone; the code itself is ~0.93" square at the default address
QR_BORDER = 3                  # quiet-zone modules inside QR_SIZE (the label's own padding adds more white)
GUTTER = 0.06 * INCH
NAME_SIZES = (14, 13, 12)      # bold; never below 12 pt
DESCRIPTION_SIZES = (10, 9.5, 9)   # try each; never below 9 pt
DESCRIPTION_SIZE = DESCRIPTION_SIZES[0]
DESCRIPTION_MIN_SIZE = DESCRIPTION_SIZES[-1]
MAX_LABELS = 450               # 30 sheets in one print; more is almost certainly a quantity typo
ASCII = str.maketrans({'\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
                       '\u2026': '...', '\u00a0': ' ', '\u00d7': 'x'})


class LabelError(Exception):
    """A plain message for staff (no lines to label, too many labels, cancelled PO)."""


def whole(quantity):
    """Pieces on a line: quantities are whole numbers in the app; a stray fraction doesn't get a label."""
    return max(int(Decimal(quantity or 0)), 0)


def label_lines(po):
    return [line for line in po.lines.all() if whole(line.quantity) > 0]


def label_count(po):
    return sum(whole(line.quantity) for line in label_lines(po))


def check_printable(po, copies=1):
    if po is None or po.kind != 'po':
        raise LabelError('Save the purchase order first.')
    if po.status == 'cancelled':
        raise LabelError(f'{po.number} was cancelled, so it has no item labels.')
    count = label_count(po)
    if not count:
        raise LabelError(f'{po.number} has no items with a quantity to label.')
    if count * copies > MAX_LABELS:
        raise LabelError(f'That is {count * copies} labels ({math.ceil(count * copies / PER_SHEET)} sheets). '
                         f'Labels print up to {MAX_LABELS} at a time; check the quantities or print fewer copies.')
    return count


@transaction.atomic
def ensure_codes(po):
    """Make the piece codes this PO's labels need, once. Existing codes are kept as they are (a reprint gives the same
    codes); a line whose quantity grew gets codes for the new pieces only. Nothing is ever deleted or renumbered."""
    PurchaseDocument.objects.select_for_update().filter(pk=po.pk).first()
    made = 0
    for line in label_lines(po):
        have = set(line.piece_labels.values_list('piece', flat=True))
        for piece in range(1, whole(line.quantity) + 1):
            if piece in have:
                continue
            code = new_code()
            while PieceLabel.objects.filter(code=code).exists():
                code = new_code()
            PieceLabel.objects.create(line=line, piece=piece, code=code)
            made += 1
    return made


def build(po):
    """Plain label data in print order (PO line order, then piece): what each label says."""
    labels = []
    for line in label_lines(po):
        count = whole(line.quantity)
        codes = {p.piece: p.code for p in line.piece_labels.filter(piece__lte=count)}
        for piece in range(1, count + 1):
            code = codes.get(piece)
            if code is None:
                raise LabelError('Piece codes are missing for this PO. Print the labels again.')
            # Serial Number & Label Standard, rule 3: the label prints the serial AND the item number in
            # plain text. The item number is the PO line's sku (the item's number); a line with no item prints (none).
            item_number = (line.sku or (line.item.sku if line.item_id and line.item else '') or '').strip() or '(none)'
            labels.append(dict(name=line.item_name or line.sku or 'Item', description=line.description or '',
                               quantity=count, piece=piece, po_number=po.number, code=code, url=piece_url(code),
                               item_number=item_number))
    return labels


def labels_for_print(po, copies=1):
    """Check, make any missing codes, and return the label data (the only step that writes)."""
    check_printable(po, copies)
    ensure_codes(po)
    return build(po)


# ---- Layout ---------------------------------------------------------------------------------------------------------

def origin(index):
    """Bottom-left corner (points, PDF coordinates) of label slot 0..14 on a sheet."""
    column, row = index % len(COLUMN_LEFT), (index // len(COLUMN_LEFT)) % len(ROW_TOP)
    return COLUMN_LEFT[column] * INCH, PAGE_H - ROW_TOP[row] * INCH - LABEL_H


def plain(text):
    return ' '.join(str(text or '').translate(ASCII).split())


def wrap(text, font, size, width):
    """Word wrap; a word wider than the line (item names like EXAMPLE-CHAIR-XL) is broken by characters."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines, current = [], ''
    for word in plain(text).split(' '):
        if not word:
            continue
        trial = f'{current} {word}' if current else word
        if stringWidth(trial, font, size) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = ''
        while stringWidth(word, font, size) > width:
            cut = len(word)
            while cut > 1 and stringWidth(word[:cut], font, size) > width:
                cut -= 1
            hyphen = word.rfind('-', 0, cut)
            if hyphen >= cut // 2:      # break after a dash when there is one in the second half
                cut = hyphen + 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines


def fit(text, font, size, width, max_lines):
    """At most max_lines lines; the last one ends in '...' when text was cut."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    lines = wrap(text, font, size, width)
    if len(lines) <= max_lines:
        return lines
    lines = lines[:max_lines]
    last = lines[-1]
    while last and stringWidth(last + '...', font, size) > width:
        last = last[:-1]
    lines[-1] = last.rstrip() + '...'
    return lines


def name_layout(name, width):
    """Biggest name size that fits one line, else the biggest that fits two; else 12 pt on up to three lines."""
    for most in (1, 2):
        for size in NAME_SIZES:
            lines = wrap(name, 'Helvetica-Bold', size, width)
            if len(lines) <= most:
                return size, lines
    return NAME_SIZES[-1], fit(name, 'Helvetica-Bold', NAME_SIZES[-1], width, 3)


def draw_qr(c, url, left, top, size):
    """QR code (error correction M) as vector squares, quiet zone included in `size`."""
    import segno
    qr = segno.make(url, error='m', micro=False, boost_error=False)
    matrix = qr.matrix
    modules = len(matrix) + 2 * QR_BORDER
    step = size / modules
    bleed = 0.04   # tiny overlap so no hairline shows between modules when the page is rasterized
    c.setFillColorRGB(0, 0, 0)
    for r, row in enumerate(matrix):
        col = 0
        while col < len(row):
            if not row[col]:
                col += 1
                continue
            start = col
            while col < len(row) and row[col]:
                col += 1
            c.rect(left + (QR_BORDER + start) * step - bleed, top - (QR_BORDER + r + 1) * step - bleed,
                   (col - start) * step + 2 * bleed, step + 2 * bleed, stroke=0, fill=1)
    return qr


def draw_label(c, label, x, y, marks=False):
    top = y + LABEL_H
    left = x + PAD
    draw_qr(c, label['url'], left, top - PAD, QR_SIZE)
    qr_bottom = top - PAD - QR_SIZE
    c.setFillColorRGB(0, 0, 0)

    right = left + QR_SIZE + GUTTER
    width = x + LABEL_W - PAD - right
    size, lines = name_layout(label['name'], width)
    leading = size * 1.1
    baseline = top - PAD - size * 0.8
    c.setFont('Helvetica-Bold', size)
    for line in lines:
        c.drawString(right, baseline, line)
        baseline -= leading
    baseline -= 2
    for font, fsize, text in (('Helvetica-Bold', 11, fit(label['po_number'], 'Helvetica-Bold', 11, width, 1)[0]),
                              ('Helvetica', 10, f'PO Quantity: {label["quantity"]}'),
                              ('Helvetica-Bold', 10, f'Piece {label["piece"]} of {label["quantity"]}')):
        baseline -= fsize * 0.2
        c.setFont(font, fsize)
        c.drawString(right, baseline, fit(text, font, fsize, width, 1)[0])
        baseline -= fsize * 1.15

    # Under the QR, full width: fit as much of the description as possible.
    # Shrink to DESCRIPTION_MIN_SIZE before truncating with "...".
    full = LABEL_W - 2 * PAD
    top_line = qr_bottom - 4
    for fsize in DESCRIPTION_SIZES:
        leading = fsize * 1.15
        room = max(1, int((top_line - fsize - (y + PAD)) // leading) + 1)
        lines = wrap(label['description'], 'Helvetica', fsize, full)
        if len(lines) <= room:
            break
    else:
        fsize = DESCRIPTION_SIZES[-1]
        leading = fsize * 1.15
        room = max(1, int((top_line - fsize - (y + PAD)) // leading) + 1)
        lines = fit(label['description'], 'Helvetica', fsize, full, room)
    c.setFont('Helvetica', fsize)
    baseline = top_line - fsize
    for line in lines:
        c.drawString(left, baseline, line)
        baseline -= leading
    if marks:
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.setFont('Helvetica', 5.5)
        c.drawRightString(x + LABEL_W - PAD, y + 3, 'TESTING')
        c.setFillColorRGB(0, 0, 0)


def draw_outlines(c):
    c.saveState()
    c.setStrokeColorRGB(0.55, 0.55, 0.55)
    c.setLineWidth(0.5)
    for index in range(PER_SHEET):
        x, y = origin(index)
        c.roundRect(x, y, LABEL_W, LABEL_H, CORNER, stroke=1, fill=0)
        c.line(x + LABEL_W / 2 - 4, y + LABEL_H / 2, x + LABEL_W / 2 + 4, y + LABEL_H / 2)
        c.line(x + LABEL_W / 2, y + LABEL_H / 2 - 4, x + LABEL_W / 2, y + LABEL_H / 2 + 4)
    c.restoreState()


def pdf_bytes(labels, copies=1, outlines=False, title='PO Item Labels'):
    """Letter PDF on the SL548 grid. outlines=True adds the label outlines (plain-paper alignment check)."""
    from reportlab.pdfgen import canvas
    sequence = list(labels) * max(int(copies), 1)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H), pageCompression=1)
    c.setTitle(title)
    c.setAuthor(business().name)
    marks = testing_marks()
    for start in range(0, max(len(sequence), 1), PER_SHEET):
        if outlines:
            draw_outlines(c)
        for index, label in enumerate(sequence[start:start + PER_SHEET]):
            draw_label(c, label, *origin(index), marks=marks)
        c.showPage()
    c.save()
    return buffer.getvalue()


def alignment_pdf():
    """One sheet with only the 15 label outlines: print on plain paper and hold it against a label sheet."""
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))
    c.setTitle('SL548 label alignment test')
    draw_outlines(c)
    c.setFont('Helvetica', 7)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 0.16 * INCH,
                        'SL548 alignment test: print on plain paper and hold it against a label sheet up to the light.')
    for index in range(PER_SHEET):
        x, y = origin(index)
        c.drawCentredString(x + LABEL_W / 2, y + LABEL_H / 2 - 14, f'Label {index + 1}')
    c.showPage()
    c.save()
    return buffer.getvalue()
