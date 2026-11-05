# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inventory On Hand: every inventory item's current on-hand quantity and value, grouped by department.

Source is the app's Item table as it stands now (QB POS on-hand at backup time plus any native sales/receiving since).
This is a current snapshot: the backup has no stock-movement history, so an "as of a past date" view is not offered.

Whole cents only: Avg Cost is rounded half-up to two decimals and Ext Cost = On Hand × that cost, also rounded half-up. This
matches the "Inventory Valuation" export layout (Department, Item Name, Item # Ext Cost) with quantity and cost
columns added. Negative on-hand quantities (oversold in QB) are shown as they are and flagged in a note. Items in the
Items Trash appear only while they hold stock (received or returned while trashed), labelled "(in Items Trash)".
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F, Q

from sales.models import Item

CENT = Decimal('0.01')
ZERO = Decimal('0.00')
NO_DEPARTMENT = '(No department)'


def cents(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Row:
    sku: str
    name: str
    description: str
    vendor: str
    on_hand: Decimal
    cost: Decimal
    value: Decimal
    price: Decimal | None
    trashed: bool = False


TRASH_LABEL = '(in Items Trash)'


@dataclass
class Group:
    department: str
    rows: list = field(default_factory=list)
    items: int = 0
    on_hand: Decimal = Decimal('0')
    value: Decimal = ZERO

    def add(self, row):
        self.rows.append(row)
        self.items += 1
        self.on_hand += row.on_hand
        self.value += row.value


@dataclass
class Report:
    include_zero: bool
    groups: list
    items: int
    on_hand: Decimal
    value: Decimal
    negative_items: int
    skipped_zero: int
    trashed_items: int = 0

    title = 'Inventory On Hand'

    @property
    def subtitle(self):
        return 'All inventory items' if self.include_zero else 'Items with stock on hand'


def build(include_zero=False):
    # Items in the Items Trash normally hold nothing: their stock was zeroed on the way in. One that was received
    # on an existing PO line or restocked by a return while trashed does hold real stock, so it is listed and labelled
    # "(in Items Trash)" rather than left out of the valuation; trashed items at zero stay out even with include_zero.
    items = (Item.objects.filter(item_type='inventory').filter(Q(active=True) | ~Q(on_hand=0))
             .annotate(department_name=F('department__name'), vendor_name=F('primary_vendor__company'))
             .order_by('department_name', 'sku')
             .values('sku', 'name', 'description', 'vendor_name', 'department_name', 'on_hand', 'average_cost', 'price', 'active'))
    groups, by_name = [], {}
    total_qty, total_value, count, negatives, skipped, trashed = Decimal('0'), ZERO, 0, 0, 0, 0
    for r in items.iterator(chunk_size=2000):
        qty = r['on_hand'] or Decimal('0')
        if qty == 0 and not r['active']:
            continue
        if qty == 0 and not include_zero:
            skipped += 1
            continue
        cost = cents(r['average_cost'])
        value = cents(qty * cost)
        name = r['department_name'] or NO_DEPARTMENT
        group = by_name.get(name)
        if group is None:
            group = by_name[name] = Group(name)
            groups.append(group)
        label = r['name'] if r['active'] else f"{r['name']} {TRASH_LABEL}".strip()
        group.add(Row(r['sku'], label, r['description'], r['vendor_name'] or '', qty, cost, value, r['price'], trashed=not r['active']))
        total_qty += qty
        total_value += value
        count += 1
        negatives += qty < 0
        trashed += not r['active']
    groups.sort(key=lambda g: (g.department == NO_DEPARTMENT, g.department.casefold()))
    return Report(include_zero, groups, count, total_qty, total_value, negatives, skipped, trashed)
