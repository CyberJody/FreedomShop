# SPDX-License-Identifier: AGPL-3.0-or-later
"""Continuous-scroll row batches shared by the document, vendor and department lists.

The item and customer lists have their own, earlier implementations; this module
gives every other list screen the same behaviour with one JSON shape:
``{rows: [{id, name, href, token?, cells}], offset, total}``. A cell is a plain
string or a list of segments, where a segment is a string or ``{text, href}``.
"""
from decimal import Decimal
from urllib.parse import urlencode

from django.http import JsonResponse
from django.utils import timezone

BATCH = 100
PAGE = 50
DROPPED = ('page', 'at', 'view', 'format', 'offset', 'random')


def wants_rows(request):
    return request.GET.get('format') == 'rows'


def scroll_rows(request, queryset, build, prepare=None):
    raw = request.GET.get('offset', '0')
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        return JsonResponse({'error': 'Invalid list offset.'}, status=400)
    offset = int(raw)
    batch = list(queryset[offset:offset + BATCH])
    if prepare:
        batch = prepare(batch)
    rows = [build(record) for record in batch]
    return JsonResponse(dict(rows=rows, offset=offset, total=queryset.count()))


def scroll_config(request, page, columns, **extra):
    params = [(key, value) for key, value in request.GET.lists() for value in value if key not in DROPPED]
    return dict(total=page.paginator.count, start=page.start_index() - 1 if page.object_list else 0,
                columns=columns, params=urlencode(params), paged=request.GET.get('view') == 'pages', **extra)


def link(text, href):
    return {'text': str(text), 'href': href}


def fmt_date(value):
    if not value:
        return '—'
    if hasattr(value, 'tzinfo') and value.tzinfo is not None:
        value = timezone.localtime(value)
    return f'{value.month}/{value.day}/{value.year}'


def qty(value):
    """Match the template filter floatformat:'-3'."""
    if value is None:
        return ''
    text = f'{Decimal(value):.3f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


def money(value):
    return '' if value is None else f'${Decimal(value):.2f}'
