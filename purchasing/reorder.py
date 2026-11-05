# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reorder Reminders (QB POS Inventory > Reminders > Reorder Items).

QB rule (help `inven434.htm`, company inventory preference): an item is listed when
    on hand + on order (open POs) - committed (open customer orders) - held <= reorder point ("Available quantity", QB default)
    on hand + on order <= reorder point ("On-hand quantity")
"Held" (not in QB) is what app-made held receipts for real customers reserve; see sales.holds.
Only items with a reorder point above 0 count; inactive and Unorderable items are left off (QB: flag an item
unorderable to keep it off the reminders). Checked against the 9/27 backup: the default rule lists exactly the 12 items
QB itself flags `BelowReorder`.

"Ignore" is per item and temporary: it hides the item from the reminders (and the Home count) until the item is back
above its reorder point, then clears itself so the next shortage shows again.
"""
from decimal import Decimal

from django.db.models import Q

from sales.models import Item, Line
from sales.holds import held_by_item  # Available = on hand - committed - held
from .catalog_quantities import catalog_quantities
from .models import ReorderIgnore

RULES = {'available': 'Available quantity', 'on_hand': 'On-hand quantity'}
DEFAULT_RULE = 'available'
ZERO = Decimal('0')


def candidates():
    return (Item.objects.filter(active=True, unorderable=False, reorder_point__gt=0)
            .select_related('primary_vendor').defer('photo', 'comments'))


def committed_by_item(item_ids):
    """Quantity still owed on open customer orders (ordered - picked up), per item. Same rule as the Item Detail."""
    committed = {}
    lines = Line.objects.filter(item_id__in=item_ids, document__kind='order', document__status='open')
    for line in lines:
        remaining = max(line.quantity - line.fulfilled, ZERO)
        if remaining:
            committed[line.item_id] = committed.get(line.item_id, ZERO) + remaining
    return committed


def evaluate(rule=DEFAULT_RULE):
    """Every item with a reorder point, with its quantities and whether it is at/below the point under `rule`."""
    if rule not in RULES:
        rule = DEFAULT_RULE
    items = list(catalog_quantities(candidates()))
    committed = committed_by_item([i.pk for i in items])
    held = held_by_item([i.pk for i in items])
    ignored = set(ReorderIgnore.objects.filter(item__in=items).values_list('item_id', flat=True))
    rows = []
    for item in items:
        on_hand = item.on_hand or ZERO
        on_order = item.on_order or ZERO
        owed = committed.get(item.pk, ZERO)
        reserved = held.get(item.pk, ZERO)
        projected = on_hand + on_order - (owed + reserved if rule == 'available' else ZERO)
        rows.append(dict(item=item, on_hand=item.on_hand, on_order=on_order, committed=owed, held=reserved, available=on_hand - owed - reserved,
                         projected=projected, reorder_point=item.reorder_point,
                         below=projected <= item.reorder_point, ignored=item.pk in ignored,
                         vendor=item.primary_vendor))
    return rows


def reminders(rule=DEFAULT_RULE):
    """(listed, ignored): items at or below their reorder point, split by the per-item Ignore flag."""
    below = [row for row in evaluate(rule) if row['below']]
    key = sort_key
    listed = sorted((r for r in below if not r['ignored']), key=key)
    ignored = sorted((r for r in below if r['ignored']), key=key)
    return listed, ignored


def sort_key(row):
    return ((row['vendor'].code if row['vendor'] else '~'), row['item'].display_name.lower())


def reminder_count(rule=DEFAULT_RULE):
    return len(reminders(rule)[0])


def clear_restocked(rule=DEFAULT_RULE):
    """Drop Ignore flags on items that are back above their reorder point (the ignore was only temporary)."""
    back = [row['item'].pk for row in evaluate(rule) if row['ignored'] and not row['below']]
    stale = ReorderIgnore.objects.filter(Q(item_id__in=back) | Q(item__reorder_point__isnull=True) | Q(item__reorder_point__lte=0))
    return stale.delete()[0]


def set_ignored(item, ignored):
    if ignored:
        ReorderIgnore.objects.get_or_create(item=item)
    else:
        ReorderIgnore.objects.filter(item=item).delete()


def by_vendor(rows):
    """Group rows by primary vendor for the list (one Create PO button per vendor)."""
    groups = []
    for row in rows:
        vendor = row['vendor']
        if not groups or groups[-1]['vendor'] != vendor:
            groups.append(dict(vendor=vendor, rows=[]))
        groups[-1]['rows'].append(row)
    return groups
