# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only item-list batches and navigation state for the item list."""
from shop_settings.permissions import register_can
from urllib.parse import parse_qsl, urlencode, urlsplit
from django.http import JsonResponse

from sales.models import money, quantity_text


def item_return_url(value):
    try:
        parts = urlsplit(value[:2000])
    except ValueError:
        return ''
    if parts.scheme or parts.netloc or parts.path != '/purchasing/items/':
        return ''
    params = {key: val for key, val in parse_qsl(parts.query)
              if key in {'q', 'source', 'status', 'page', 'at', 'view', 'sort'}}
    return '/purchasing/items/?' + urlencode(params)


def item_scroll_config(request, queryset, page, source, query, status='all'):
    names = [] if request.GET.get('view') == 'pages' else [
        name.strip() or 'Unnamed item' for name in queryset.values_list('name', flat=True).iterator(chunk_size=2000)]
    return dict(source=source, q=query, status=status, sort=request.GET.get('sort',''), names=names,
                total=page.paginator.count, start=page.start_index() - 1 if page.object_list else 0,
                columns=17)


def item_rows(request, queryset, source):
    raw = request.GET.get('offset', '0')
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        return JsonResponse({'error': 'Invalid item offset.'}, status=400)
    offset = int(raw)
    rows = []
    def value(number, fallback='Unverified'):
        # Fallback first, then whole-number text: quantity_text(None) is '' and would lose the label (review).
        return quantity_text(number) if number is not None else fallback
    for item in queryset[offset:offset + 100]:
        name = item.name.strip() or 'Unnamed item'
        average = f'${money(item.average_cost):.2f}' if item.average_cost is not None else item.average_cost_missing_label
        cells = [item.display_name, item.description, value(item.on_hand), value(getattr(item, 'held', None), '0'), f'${money(item.price):.2f}' if item.price is not None else 'Enter price', value(item.on_order, '0'),
                 item.last_received.strftime('%m/%d/%Y') if item.last_received else '—', item.size, str(item.primary_vendor or '—'), str(item.department or '—'),
                 f'${money(item.order_cost):.2f}' if item.order_cost is not None else 'Unverified', average, item.upc, item.alu, value(item.tax_rate), 'Active' if item.active else 'In Trash', item.sku]
        if not register_can(request, 'see_costs'):
            cells[10:12] = ['', '']
        href = f'/purchasing/items/{item.pk}/'
        rows.append(dict(id=item.pk, name=name, cells=cells, href=href,
                         orders=bool(item.on_order and item.on_order > 0)))  # the On Order cell opens the item's POs
    return JsonResponse(dict(rows=rows, offset=offset, total=queryset.count()))
