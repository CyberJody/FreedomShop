# SPDX-License-Identifier: AGPL-3.0-or-later
"""what a customer order line still needs ordered from a vendor.

QuickBooks POS never stored which PO line was for which customer order line, so every PO brought over from QB has no
link to the order (PurchaseLine.sales_line is empty). Generate PO used to deduct only linked PO lines and so offered
QB-era special-order pieces again. Order lines with no linked PO line are now matched to POs by item:

* Special-order items (a shop's "SOLD" pieces: "SOLD" in the item name, "(SOLD …" in the description, or the
  SPECIAL ORDER department) belong to one customer, so any non-cancelled PO quantity of that item (ordered, open,
  closed or received; QB-era or app-made) covers that customer's order lines, oldest order first. Only the remainder
  is offered. An item ordered by more than one customer is not treated as a special-order piece.
* Stock items are shared by many customers, so a PO for one says nothing about a particular order. On an order brought
  over from QuickBooks, a stock line counts as covered when the item has enough on hand, or when a non-cancelled PO
  line for the item is dated on or after the order date. Those lines are left off the PO with the reason shown, so
  staff can still add them by hand. Stock lines on app-made orders keep the old rule (only linked POs count).
* Lines with a linked PO line keep the old rule exactly (procurement_capacity).

QuickBooks deleted some special-order items after they were received, so an imported order line can have no item; it
is matched by its QB item number (Line.sku = PurchaseLine.sku), which QB never reused.
"""
from config.provenance import native_source
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .models import PurchaseLine
from .procurement import procurement_capacity

SOLD_WORD = re.compile(r'\bSOLD\b', re.IGNORECASE)
ZERO = Decimal('0')


@dataclass
class Coverage:
    remaining: Decimal
    reason: str = ''                     # why less than the open quantity is offered ('' when nothing is deducted)
    purchase_orders: list = field(default_factory=list)   # PurchaseDocuments matched by item (special-order pieces)


def sold_marked(name, description):
    return bool(SOLD_WORD.search(name or '')) or '(SOLD' in (description or '').upper()


def _special_department(item):
    department = item.department
    return bool(department and ((department.code or '').upper() == 'SPE' or (department.name or '').strip().lower() == 'special order'))


def special_item_ids(items):
    """The ids of the given items that are special-order pieces (marked SOLD or in SPECIAL ORDER, and on no more than
    one customer's orders)."""
    from sales.models import Line
    marked = {i.pk for i in items if i is not None and (sold_marked(i.name, i.description) or _special_department(i))}
    if not marked:
        return set()
    customers = defaultdict(set)
    for item_id, customer_id, name in Line.objects.filter(item_id__in=marked, document__kind='order').values_list(
            'item_id', 'document__customer_id', 'document__customer_name'):
        customers[item_id].add(customer_id or (name or '').strip().upper())
    return {pk for pk in marked if len(customers[pk]) <= 1}


def _key(line):
    if line.item_id:
        return ('item', line.item_id)
    if line.sku and line.document.from_quickbooks:
        return ('sku', line.sku)
    return None


def _po_lines(key):
    qs = PurchaseLine.objects.filter(document__kind='po').exclude(document__status='cancelled').select_related('document')
    return qs.filter(item_id=key[1]) if key[0] == 'item' else qs.filter(item__isnull=True, sku=key[1])


def _order_lines(key):
    from sales.models import Line
    qs = Line.objects.filter(document__kind='order', document__status='open', quantity__gt=0).select_related('document')
    return qs.filter(item_id=key[1]) if key[0] == 'item' else qs.filter(~native_source('document__source_id'), item__isnull=True, sku=key[1])


def linked_line_ids(lines):
    return set(PurchaseLine.objects.filter(sales_line__in=lines, document__kind='po').values_list('sales_line_id', flat=True))


def coverage(lines):
    """{sales line pk: Coverage} for the given customer-order lines."""
    from sales.models import quantity_text
    lines = [l for l in lines]
    linked = linked_line_ids(lines)
    specials = special_item_ids([l.item for l in lines if l.item_id and l.pk not in linked])
    result = {}
    special_keys = {}
    for line in lines:
        if line.pk in linked:
            result[line.pk] = Coverage(procurement_capacity(line))
            continue
        key = _key(line)
        special = key is not None and (line.item_id in specials if line.item_id else sold_marked(line.item_name, line.description))
        if special:
            special_keys.setdefault(key, []).append(line)
            continue
        open_qty = max(line.quantity - line.fulfilled, ZERO)
        if not line.document.from_quickbooks or key is None or open_qty <= 0:
            result[line.pk] = Coverage(procurement_capacity(line))
            continue
        # A stock item on an order brought over from QuickBooks.
        item = line.item
        if item is not None and item.on_hand is not None and item.on_hand >= open_qty:
            result[line.pk] = Coverage(ZERO, f'in stock ({quantity_text(item.on_hand)} on hand)')
            continue
        order_day = timezone.localdate(line.document.created_at)
        later = [p for p in _po_lines(key) if timezone.localdate(p.document.created_at) >= order_day]
        if later:
            po = max(later, key=lambda p: p.document.created_at).document
            result[line.pk] = Coverage(ZERO, f'on {po.number} dated {timezone.localdate(po.created_at):%-m/%-d/%Y}, on or after this order')
            continue
        result[line.pk] = Coverage(procurement_capacity(line))
    for key, wanted in special_keys.items():
        po_lines = [p for p in _po_lines(key) if p.sales_line_id is None]
        pool = sum((p.quantity for p in po_lines), ZERO)
        documents = list({p.document_id: p.document for p in po_lines}.values())
        # Every open order line for this piece that has no linked PO line shares the pool, oldest order first.
        rivals = [l for l in _order_lines(key)]
        rival_linked = linked_line_ids(rivals)
        queue = sorted({l.pk: l for l in [*rivals, *wanted] if l.pk not in rival_linked}.values(),
                       key=lambda l: (l.document.created_at, l.document_id, l.pk))
        for line in queue:
            take = min(pool, max(line.quantity, ZERO))
            pool -= take
            if line not in wanted:
                continue
            remaining = max(line.quantity - max(line.fulfilled, take), ZERO)
            reason = ''
            if take > 0:
                numbers = ', '.join(d.number for d in documents[:3]) + (' …' if len(documents) > 3 else '')
                reason = f'{quantity_text(take)} already on {numbers}'
            result[line.pk] = Coverage(remaining, reason, documents)
    return result


def matched_purchase_orders(order):
    """POs that hold this order's special-order pieces without a link (QB-era POs, or one typed by hand)."""
    from sales.models import Line
    from .models import PurchaseDocument
    lines = list(Line.objects.filter(document=order).select_related('item__department', 'document'))
    linked = linked_line_ids(lines)
    specials = special_item_ids([l.item for l in lines if l.item_id and l.pk not in linked])
    match = Q()
    found = False
    for line in lines:
        if line.pk in linked:
            continue
        if line.item_id and line.item_id in specials:
            match |= Q(lines__item_id=line.item_id); found = True
        elif not line.item_id and line.sku and order.from_quickbooks and sold_marked(line.item_name, line.description):
            match |= Q(lines__item__isnull=True, lines__sku=line.sku); found = True
    if not found:
        return PurchaseDocument.objects.none()
    return PurchaseDocument.objects.filter(match, kind='po').distinct()


def orders_for_purchase_lines(po_lines):
    """{purchase document pk: [orders]} for unlinked PO lines that hold a special-order piece: the open customer
    order(s) that piece is on (the PO List's Customer Order column)."""
    from sales.models import Line
    po_lines = [p for p in po_lines if p.sales_line_id is None and p.item_id]
    if not po_lines:
        return {}
    items = {p.item_id: p.item for p in po_lines}
    specials = special_item_ids(list(items.values()))
    if not specials:
        return {}
    by_item = defaultdict(list)
    for line in Line.objects.filter(item_id__in=specials, document__kind='order', document__status='open').select_related('document'):
        if line.document not in by_item[line.item_id]:
            by_item[line.item_id].append(line.document)
    found = defaultdict(list)
    for p in po_lines:
        for order in by_item.get(p.item_id, []):
            if order not in found[p.document_id]:
                found[p.document_id].append(order)
    return found
