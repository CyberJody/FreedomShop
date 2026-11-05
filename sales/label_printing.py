# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zebra item price tags (GX420t, 203 dpi), drawn like a shop's current tags.

The tag reads portrait, 3 in wide x 4 in tall, with the label perforations on its left and right sides. The Zebra
feeds 4 in across the head (812 dots) and ~3 in per label along the feed (599 dots), so the tag is drawn on a
599 x 812 portrait canvas and turned 90 degrees onto the 812 x 599 label. One renderer feeds both the on-screen
preview PNG and the printer (ZPL ^GFA bitmap, Zebra ASCII compression), so the preview is exactly what prints.

Top to bottom: logo, item name, item code line, "Made in the USA" alignment seal (staff stick the color sticker
over it), WOOD:, SIZE:, FINISHED PRICE, price with small raised cents. Logo, seal, FINISHED PRICE and the price
keep fixed positions; name / code / wood / size wrap or shrink to fit their boxes. No barcode.

The Letter (Brother) tag keeps its own layout: `tag_fields()` below, drawn by `letter_printing.tag_pdf()`.
"""
import math
import re
import textwrap
from dataclasses import asdict, dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.template.defaultfilters import floatformat
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps

DPI = 203
TAG_W, TAG_H = 599, 812          # portrait tag as it hangs in the sleeve (3 x 4 in)
LABEL_W, LABEL_H = 812, 599      # the label as the Zebra prints it (^PW812 ^LL599, from ^HH on a shop's GX420t)
# Which way the portrait tag is turned onto the label. 'cw' = 90 degrees clockwise (ZPL "R" direction): the top of
# the tag lies along the label's right edge (x = 812) in ZPL coordinates. Use 'ccw' to turn it the other way.
TAG_ROTATION = 'cw'
SIDE = 16                        # side margin inside the tag (the perforation edges)
TEXT_W = TAG_W - 2 * SIDE

# "Made in the USA" seal: staff stick a ~1 in color sticker on every tag. We print a black-and-white guide 20 %
# smaller than the sticker at the sticker's spot, so the sticker covers it. Adjust these after a real print.
SEAL_STICKER_IN = 0.9            # sticker diameter estimated from the photos (0.83-0.95 in against the 3 in tag width)
SEAL_SCALE = 0.8                 # guide = 80 % of the sticker
SEAL_DIAMETER = round(SEAL_STICKER_IN * DPI * SEAL_SCALE)   # 146 dots = 0.72 in
SEAL_CENTER = (TAG_W // 2, 377)   # nominal spot: sets the room for the text above and below (sizing only).
# The seal is drawn centered in the actual space left between the last line of name/code text and the first WOOD/SIZE
# line, so it sits balanced whether there is one line above it or four.
SEAL_GAP = 6                      # minimum clearance between the seal and any text

# Positions measured from the photos (IMG_2820 scaled to 599 x 812).
LOGO_TOP, LOGO_W = 10, 330       # saw-blade logo, ~57 % of the tag width; 10 dots clear of the label edge
NAME_TOP = 194                   # name + code block: under the logo, above the seal
WOOD_BOTTOM = 571                # WOOD / SIZE block sits right above FINISHED PRICE
FINISHED_TOP = 575               # FINISHED PRICE, fixed
FINISHED_SIZE = 64
PRICE_BASELINE = 762             # price baseline, fixed
PRICE_SIZE, CENTS_RATIO = 132, 0.42

FONT_DIR = Path(__file__).resolve().parent / 'print_assets' / 'fonts'
REGULAR, BOLD = 'DejaVuSansCondensed.ttf', 'DejaVuSansCondensed-Bold.ttf'
WIDE = 'DejaVuSans.ttf'          # name and code lines: the photos use a wider, lighter face there

LIMITS = dict(name=120, code=80, wood=80, size=80, price=20)
FIELD_LABELS = dict(name='Name', code='Item code', wood='Wood', size='Size', price='Price')


@dataclass
class TagText:
    name: str = ''
    code: str = ''
    wood: str = ''
    size: str = ''
    price: str = ''

    def as_dict(self):
        return asdict(self)


def clean(value):
    return ' '.join(str(value or '').split())


@lru_cache(maxsize=64)
def font(name, size):
    return ImageFont.truetype(str(FONT_DIR / name), size)


# ---------------------------------------------------------------- prefill from the Item List

WOOD_ABBREVIATIONS = {'RHICK': 'RUSTIC HICKORY', 'RUSTIC HICK': 'RUSTIC HICKORY', 'RUSTIC HICKO': 'RUSTIC HICKORY',
                      'R HICK': 'RUSTIC HICKORY', 'R. HICK': 'RUSTIC HICKORY', 'R CHERRY': 'RUSTIC CHERRY',
                      'RQSWO': 'RUSTIC QUARTERSAWN WHITE OAK', 'QSWO': 'QUARTERSAWN WHITE OAK', 'RWAL': 'RUSTIC WALNUT',
                      'BR. MAPLE': 'BROWN MAPLE', 'BR MAPLE': 'BROWN MAPLE', 'B MAPLE': 'BROWN MAPLE'}
# Longest first, so "RUSTIC HICKORY" wins over "HICKORY". Finish names that contain a wood ("MICHAELS CHERRY")
# are skipped by the look-behind in WOOD_RE.
WOODS = sorted(['RUSTIC HICKORY', 'NATURAL HICKORY', 'HICKORY', 'RUSTIC QUARTERSAWN WHITE OAK', 'QUARTERSAWN WHITE OAK',
                'RUSTIC QUARTERSAWN OAK', 'QUARTERSAWN OAK', 'WHITE OAK', 'RED OAK', 'RUSTIC OAK', 'OAK BARREL STAVES',
                'OAK', 'BROWN MAPLE', 'HARD MAPLE', 'RUSTIC MAPLE', 'MAPLE', 'RUSTIC CHERRY', 'CHERRY', 'PRIME WALNUT',
                'RUSTIC WALNUT', 'WALNUT', 'RECLAIMED BARNWOOD', 'BARNWOOD', 'ELM', 'PINE', 'POPLAR', 'ASH', 'BIRCH',
                'YELLOW LOCUST', 'TEAK', 'CEDAR'], key=len, reverse=True)
WOOD_RE = re.compile(r"(?<!MICHAELS )(?<!MICHAEL'S )\b(" + '|'.join(re.escape(w) for w in WOODS) + r')\b')
_N = r'\d+(?:\.\d+)?(?:\s*(?:"|\'\'|IN\b))?\s*[WDHL]?'
SIZE_RE = re.compile(r'(APPROX\.?\s*|~\s*)?(' + _N + r'\s*[X×]\s*' + _N + r'(?:\s*[X×]\s*' + _N + r')?)', re.I)


def code_like(text):
    text = clean(text)
    return bool(text) and ' ' not in text and bool(re.search(r'[\d/\-]', text))


def parse_wood(text):
    match = WOOD_RE.search(clean(text).upper())
    return match.group(1) if match else ''


def parse_size(text):
    match = SIZE_RE.search(clean(text).upper())
    if not match:
        return ''
    size = re.sub(r'\s*[X×]\s*', ' X ', match.group(2)).strip()
    return ('~' if match.group(1) else '') + size


def description_lines(description):
    text = str(description or '').replace('\\r', '\n').replace('\r', '\n')
    return [line.strip() for line in text.split('\n') if line.strip()]


def name_from_description(description, wood=''):
    """The first line / first sentence of the description, without stars and the leading wood words."""
    lines = description_lines(description)
    if not lines:
        return ''
    first = re.split(r'\s{3,}', lines[0])[0]
    first = re.split(r'\.\s|\.$|\s?\.\.+', clean(first))[0]
    first = clean(first.replace('*', ''))
    for prefix in filter(None, [wood, parse_wood(first)]):
        if first.upper().startswith(prefix.upper() + ' ') and len(first) > len(prefix) + 3:
            first = first[len(prefix):].strip()
            break
    return first.strip(' ,;-')


def item_wood(item):
    wood = clean(item.wood).upper()
    if re.search(r'\d', wood):          # a few rows hold a price or a size in the wood field
        wood = ''
    return WOOD_ABBREVIATIONS.get(wood, wood) or parse_wood(item.description)


def price_text(price):
    return '' if price is None else f'${price:.2f}'


def prefill(item):
    """Suggested tag text for an item: staff edit it on the print screen before printing.

    In a shop's data the QB "Item Name" holds the item code (BRF-B-52-H-N, MWC-NEW"LINCOLN"-DM): that is the code
    line on the photographed tags, and the tag's name line comes from the description."""
    wood = item_wood(item)
    if code_like(item.name) or not clean(item.name):
        name = name_from_description(item.description, wood) or clean(item.name) or clean(item.sku)
        code = clean(item.name) or clean(item.sku)
    else:
        name, code = clean(item.name), clean(item.sku)
    size = clean(item.size) or parse_size(item.description)
    return TagText(name=name[:LIMITS['name']], code=code[:LIMITS['code']], wood=wood[:LIMITS['wood']],
                   size=size[:LIMITS['size']], price=price_text(item.price))


def from_data(data, fallback=None):
    """TagText from posted / query fields. Raises ValueError with a staff-readable message."""
    values = {}
    for key, limit in LIMITS.items():
        raw = data.get(key)
        if raw is None:
            raw = getattr(fallback, key, '') if fallback is not None else ''
        if any(ord(c) < 32 and c not in '\t\r\n' for c in raw) or len(raw) > 4 * limit:
            raise ValueError(f'{FIELD_LABELS[key]} has characters that cannot be printed.' if len(raw) <= 4 * limit
                             else f'{FIELD_LABELS[key]} is too long (up to {limit} characters).')
        value = clean(raw)
        if len(value) > limit:
            raise ValueError(f'{FIELD_LABELS[key]} is too long (up to {limit} characters).')
        values[key] = value
    return TagText(**values)


# ---------------------------------------------------------------- drawing

def text_width(text, face):
    return face.getlength(text)


def wrap_words(text, face, width):
    """Greedy word wrap by measured width; a single word wider than the box is broken by characters."""
    lines, line = [], ''
    for word in text.split():
        candidate = f'{line} {word}'.strip()
        if text_width(candidate, face) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ''
        while text_width(word, face) > width:
            cut = len(word) - 1
            while cut > 1 and text_width(word[:cut], face) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def ellipsize(line, face, width):
    if text_width(line, face) <= width:
        return line
    while line and text_width(line + '…', face) > width:
        line = line[:-1]
    return line.rstrip() + '…'


def line_height(size):
    return round(size * 1.12)


def draw_center(draw, y, text, face):
    """Draw text centered, top of the line box at y (ascender-based, so lines stack evenly)."""
    draw.text((TAG_W / 2, y), text, font=face, fill=0, anchor='ma')


def name_block(text):
    """Name (up to 2 lines) + code (up to 2 lines), largest sizes that fit between the logo and the seal."""
    room = SEAL_CENTER[1] - SEAL_DIAMETER // 2 - 6 - NAME_TOP
    for name_size in range(44, 17, -2):
        code_size = max(16, round(name_size * 0.68))
        name_lines = wrap_words(text.name, font(WIDE, name_size), TEXT_W)
        code_lines = wrap_words(text.code, font(WIDE, code_size), TEXT_W)
        height = len(name_lines) * line_height(name_size) + len(code_lines) * line_height(code_size)
        if len(name_lines) <= 2 and len(code_lines) <= 2 and height <= room:
            return name_size, name_lines, code_size, code_lines
    name_size, code_size = 18, 16
    name_lines = wrap_words(text.name, font(WIDE, name_size), TEXT_W)
    if len(name_lines) > 2:
        name_lines = name_lines[:2]
        name_lines[1] = ellipsize(name_lines[1] + ' …', font(WIDE, name_size), TEXT_W)
    code = clean(text.code)
    code_lines = [ellipsize(code, font(WIDE, code_size), TEXT_W)] if code else []
    return name_size, name_lines, code_size, code_lines


def label_lines(label, value, size, width):
    """Lines of "LABEL: value" — bold label, regular value; wraps the value onto extra lines when needed."""
    bold, regular = font(BOLD, size), font(REGULAR, size)
    lead = text_width(label + ' ', bold)
    first, rest = [], value.split()
    while rest and text_width(' '.join(first + rest[:1]), regular) <= width - lead:
        first.append(rest.pop(0))
    if not first and rest:   # one very long word: break it
        piece = wrap_words(rest[0], regular, width - lead)[0]
        first, rest = [piece], ([rest[0][len(piece):]] if rest[0][len(piece):] else []) + rest[1:]
    more = wrap_words(' '.join(rest), regular, width) if rest else []
    return [(label, ' '.join(first))] + [('', line) for line in more]


def wood_block(text):
    rows = [(label, value) for label, value in (('WOOD:', text.wood), ('SIZE:', text.size)) if value]
    room = WOOD_BOTTOM - (SEAL_CENTER[1] + SEAL_DIAMETER // 2 + 6)
    for size in range(32, 15, -2):
        lines = [line for label, value in rows for line in label_lines(label, value, size, TEXT_W)]
        if len(lines) <= 4 and len(lines) * line_height(size) <= room:
            return size, lines
    size, lines = 16, []
    for label, value in rows:
        lead = text_width(label + ' ', font(BOLD, size))
        lines.append((label, ellipsize(value, font(REGULAR, size), TEXT_W - lead)))
    return size, lines


def draw_label_line(draw, y, label, value, size):
    bold, regular = font(BOLD, size), font(REGULAR, size)
    lead = text_width(label + ' ', bold) if label else 0
    width = lead + text_width(value, regular)
    x = (TAG_W - width) / 2
    if label:
        draw.text((x, y), label, font=bold, fill=0, anchor='la')
    draw.text((x + lead, y), value, font=regular, fill=0, anchor='la')


def split_price(text):
    """('$1374', '95') for money text; (text, '') for anything else (e.g. "CALL"), shown large as typed."""
    match = re.fullmatch(r'\$?\s*([\d,]+)(?:\.(\d{1,2}))?', text.strip())
    if not match:
        return text.strip(), ''
    return '$' + match.group(1), (match.group(2) or '00').ljust(2, '0')


def price_layout(text):
    """(dollar text, cents text, dollar size, cents size); shrinks only when the price is too wide."""
    dollars, cents = split_price(text)
    size = PRICE_SIZE
    while size > 30:
        width = text_width(dollars, font(BOLD, size)) + (6 + text_width(cents, font(BOLD, round(size * CENTS_RATIO))) if cents else 0)
        if width <= TEXT_W:
            break
        size -= 4
    return dollars, cents, size, round(size * CENTS_RATIO)


def ink_rows(image, top, bottom):
    """(first, last + 1) row that has ink between top and bottom, or None when that band is blank."""
    box = ImageOps.invert(image.crop((0, top, TAG_W, bottom))).getbbox()
    return None if box is None else (top + box[1], top + box[3])


def seal_center(image):
    """Center of the seal: halfway between the bottom of the name/code text and the top of the WOOD/SIZE text.
    With no text on one side, the logo bottom (above) or FINISHED PRICE (below) is the edge instead."""
    half = SEAL_DIAMETER // 2
    above = ink_rows(image, NAME_TOP, SEAL_CENTER[1] - half)
    below = ink_rows(image, SEAL_CENTER[1] + half, FINISHED_TOP)
    upper = above[1] if above else (ink_rows(image, 0, NAME_TOP) or (0, NAME_TOP))[1]
    lower = below[0] if below else FINISHED_TOP
    cy = (upper + lower) / 2
    cy = max(upper + SEAL_GAP + half, min(lower - SEAL_GAP - half, cy))
    return SEAL_CENTER[0], round(cy)


def draw_seal(draw, center=None):
    """Black-and-white "Made in the USA" alignment guide; the color sticker goes on top of it."""
    cx, cy = center or SEAL_CENTER
    r = SEAL_DIAMETER / 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=3)
    inner = r * 0.78
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], outline=0, width=1)
    draw.text((cx, cy - inner * 0.42), 'MADE IN THE', font=font(BOLD, max(8, round(SEAL_DIAMETER * 0.11))), fill=0, anchor='mm')
    draw.text((cx, cy + inner * 0.18), 'USA', font=font(BOLD, max(10, round(SEAL_DIAMETER * 0.3))), fill=0, anchor='mm')
    for angle in range(0, 360, 30):   # star dots in the ring, like the sticker's border
        x, y = cx + math.cos(math.radians(angle)) * r * 0.89, cy + math.sin(math.radians(angle)) * r * 0.89
        draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=0)


def render_tag(text):
    """The portrait tag (599 x 812, mode '1'): exactly what prints, before turning it onto the label."""
    from .printing import logo, testing_marks
    image = Image.new('L', (TAG_W, TAG_H), 255)
    draw = ImageDraw.Draw(image)
    art = logo(LOGO_W)
    if art is not None:
        image.paste(art, ((TAG_W - art.width) // 2, LOGO_TOP))
    else:
        from shop_settings.config import business
        draw_center(draw, LOGO_TOP + 60, business().name, font(BOLD, 52))

    name_size, name_lines, code_size, code_lines = name_block(text)
    y = NAME_TOP
    for line in name_lines:
        draw_center(draw, y, line, font(WIDE, name_size))
        y += line_height(name_size)
    for line in code_lines:
        draw_center(draw, y, line, font(WIDE, code_size))
        y += line_height(code_size)

    size, lines = wood_block(text)
    y = WOOD_BOTTOM - len(lines) * line_height(size)
    for label, value in lines:
        draw_label_line(draw, y, label, value, size)
        y += line_height(size)

    draw_seal(draw, seal_center(image))   # after both text blocks, so it can center in the space between them

    draw_center(draw, FINISHED_TOP, 'FINISHED PRICE', font(BOLD, FINISHED_SIZE))

    if text.price:
        dollars, cents, size, cents_size = price_layout(text.price)
        big, small = font(BOLD, size), font(BOLD, cents_size)
        width = text_width(dollars, big) + (6 + text_width(cents, small) if cents else 0)
        x = (TAG_W - width) / 2
        draw.text((x, PRICE_BASELINE), dollars, font=big, fill=0, anchor='ls')
        if cents:   # small cents, tops level with the tops of the dollar digits
            cap_top = PRICE_BASELINE + big.getbbox('8', anchor='ls')[1]
            draw.text((x + text_width(dollars, big) + 6, cap_top - small.getbbox('8', anchor='ls')[1]), cents,
                      font=small, fill=0, anchor='ls')

    if testing_marks():
        draw.text((TAG_W / 2, TAG_H - 8), 'TESTING - NOT A VALID PRICE TAG', font=font(BOLD, 14), fill=0, anchor='md')
    return image.point(lambda v: 0 if v < 128 else 255).convert('1')


def to_label(tag):
    """Turn the portrait tag onto the 812 x 599 label."""
    method = Image.Transpose.ROTATE_270 if TAG_ROTATION == 'cw' else Image.Transpose.ROTATE_90
    return tag.transpose(method)


def png_bytes(image):
    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    return output.getvalue()


# ---------------------------------------------------------------- ZPL

def run_length(count):
    """Zebra ASCII compression repeat prefix: g..z = 20..400 in steps of 20, G..Y = 1..19."""
    out = ''
    while count >= 400:
        out += 'z'
        count -= 400
    if count >= 20:
        out += chr(ord('g') + count // 20 - 1)
        count %= 20
    if count:
        out += chr(ord('G') + count - 1)
    return out


def compress_row(row):
    """One hex row: ',' = rest of the row is 0 (white), '!' = rest is F (black), repeat counts for runs."""
    trailing = ''
    stripped = row.rstrip('0')
    if len(stripped) < len(row):
        trailing = ','
    else:
        stripped = row.rstrip('F')
        if len(stripped) < len(row):
            trailing = '!'
    out, i = [], 0
    while i < len(stripped):
        j = i
        while j < len(stripped) and stripped[j] == stripped[i]:
            j += 1
        count = j - i
        out.append((run_length(count) if count > 1 else '') + stripped[i])
        i = j
    return ''.join(out) + trailing


def gfa(image):
    """^GFA field for a mode-'1' image: 1 bits = black, rows padded to whole bytes, Zebra ASCII compression
    (':' repeats the previous row)."""
    bytes_per_row = (image.width + 7) // 8
    padded = Image.new('1', (bytes_per_row * 8, image.height), 1)
    padded.paste(image, (0, 0))
    raw = bytes(b ^ 0xFF for b in padded.tobytes())   # PIL '1': 1 = white; ZPL: 1 = black
    rows, previous = [], None
    for r in range(image.height):
        row = raw[r * bytes_per_row:(r + 1) * bytes_per_row].hex().upper()
        rows.append(':' if row == previous else compress_row(row))
        previous = row
    total = bytes_per_row * image.height
    return f'^GFA,{total},{total},{bytes_per_row},' + ''.join(rows)


def zpl_for(tag, copies=1):
    if not isinstance(copies, int) or isinstance(copies, bool) or not 1 <= copies <= 10:
        raise ValueError('Choose 1 to 10 copies.')
    return ('^XA\n^PW812\n^LL599\n^LH0,0\n^MNY\n^FO0,0' + gfa(to_label(tag)) + '^FS\n' + f'^PQ{copies}\n^XZ\n').encode('ascii')


def text_zpl(text, copies=1):
    return zpl_for(render_tag(text), copies)


def tag_zpl(item, copies=1):
    """The item's tag with the suggested text, as printed without edits."""
    return text_zpl(prefill(item), copies)


def test_tag_text():
    stamp = timezone.localtime().strftime('%m/%d/%Y %I:%M:%S %p')
    return TagText(name='PRINTER TEST TAG', code=stamp, wood='LOGO AT THE TOP', size='PERFORATIONS AT THE SIDES', price='$123.45')


def test_zpl():
    """File > Printers test tag: the real tag layout, so a test print also checks orientation and the seal spot."""
    return text_zpl(test_tag_text())


# ---------------------------------------------------------------- Letter tag (Brothers): text layout

def wrap(value, width, limit):
    lines = textwrap.wrap(clean(value), width=width, break_long_words=True, break_on_hyphens=False)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = lines[-1][:-1] + '…'
    return lines


def tag_fields(item):
    """(y, font height, font width, lines) for the Letter tag drawn by letter_printing.tag_pdf (layout)."""
    name = clean(item.display_name)
    size = 48 if len(name) <= 48 else 36
    from shop_settings.config import business
    fields = [(24, 28, 14, [business().name]),
              (72, size, size // 2, wrap(name, 30 if size == 48 else 40, 2)),
              (182, 26, 13, wrap(item.description, 56, 3)),
              (300, 84, 42, wrap('$' + floatformat(item.price, '2g'), 18, 1)),
              (418, 30, 15, wrap('Item No. ' + str(item.item_number), 48, 1))]
    if item.wood or item.size:
        fields.append((478, 26, 13, wrap(' '.join(filter(None, [item.wood, item.size])), 56, 1)))
    if item.upc:
        fields.append((536, 24, 12, wrap('UPC ' + item.upc, 60, 1)))
    return fields
