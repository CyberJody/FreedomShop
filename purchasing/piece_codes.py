# SPDX-License-Identifier: AGPL-3.0-or-later
"""Piece codes for PO item labels: one code per piece ordered, printed as a QR code on the label.

Format: ``PREFIX-XXXX-XXXX``. The shop chooses 2–4 uppercase letters/digits for new codes (default FS).
Seven random characters plus one check character, from a 30-character alphabet that leaves
out everything easy to mix up when read aloud or typed from a scuffed label: no 0/O, no 1/I/L, no U. The code itself
carries no PO or line number; the PieceLabel row stores which PO line and piece it is. That keeps the code short and
fixed-length, and it never goes stale (a PO number shown as PO-000123 today and the line order can change; the code
can't). The check character catches any single mistyped character.

The QR holds a URL on a shop's public site (settings.PIECE_LABEL_URL, default ``https://example.com/?wc={code}``)
so a phone camera opens the website; the app's own scanners accept either the URL or the bare code through
``parse_piece_code``. Receiving by scan reuses this helper.
"""
import re
import secrets
from urllib.parse import parse_qsl, urlsplit

from django.conf import settings

ALPHABET = '23456789ABCDEFGHJKMNPQRSTVWXYZ'   # 30 characters: no 0 O 1 I L U
BODY = 7                                       # random characters; one check character follows
WEIGHTS = (1, 7, 11, 13, 17, 19, 23)           # all coprime with 30, so any single wrong character changes the check
DEFAULT_URL = 'https://example.com/?wc={code}'
URL_PARAM = 'wc'


def check_char(body):
    return ALPHABET[sum(w * ALPHABET.index(c) for w, c in zip(WEIGHTS, body)) % len(ALPHABET)]


def format_code(chars, prefix=None):
    """Eight code characters -> the printed form; existing prefixes are preserved when supplied."""
    if prefix is None:
        from shop_settings.config import business
        prefix = business().serial_prefix
    return f'{prefix}-{chars[:4]}-{chars[4:]}'


def new_code():
    body = ''.join(secrets.choice(ALPHABET) for _ in range(BODY))
    return format_code(body + check_char(body))


def normalize(text):
    """Canonical form of a bare code, or None. Case, spaces and dashes don't matter."""
    if not isinstance(text, str):
        return None
    chars = ''.join(text.split()).replace('-', '').upper()
    prefix, chars = chars[:-(BODY + 1)], chars[-(BODY + 1):]
    if re.fullmatch(r'[A-Z0-9]{2,4}', prefix) is None or len(chars) != BODY + 1:
        return None
    if any(c not in ALPHABET for c in chars) or check_char(chars[:BODY]) != chars[BODY]:
        return None
    return format_code(chars, prefix)


def piece_url(code):
    """What the QR code holds: the shop's website address with the piece code in it (Business Settings' label address,
    else settings.PIECE_LABEL_URL)."""
    from shop_settings.config import business
    template = business().label_url or getattr(settings, 'PIECE_LABEL_URL', '') or DEFAULT_URL
    if '{code}' in template:
        return template.replace('{code}', code)
    return f'{template}{"&" if "?" in template else "?"}{URL_PARAM}={code}'


def parse_piece_code(text):
    """The piece code in a scan or typed entry, or None.

    Accepts the bare code (any case, with or without dashes) or a URL from the label's QR code: the ``wc=`` query
    parameter on any host or path, among other parameters. Surrounding whitespace and the scanner's Enter are ignored.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > 2000:
        return None
    if '=' in text or '/' in text or '?' in text:
        try:
            query = urlsplit(text).query
        except ValueError:
            return None
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key.lower() == URL_PARAM:
                code = normalize(value)
                if code:
                    return code
        return None
    return normalize(text)
