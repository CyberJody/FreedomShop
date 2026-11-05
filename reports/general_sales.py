# SPDX-License-Identifier: AGPL-3.0-or-later
"""General Sales: receipt lines for catalog items in a period, with user-selected columns and optional grouping.

Same line rules as Sales by Vendor: completed sales receipts only; lines linked to a catalog item (items deleted in
QB POS and "Payment On Account" lines are excluded); returns negative; whole cents (unit price shown rounded half-up to
cents, extension = qty × exact price, rounded half-up). Amount is what the line sold for: that extension less the line's
share of a receipt-level discount (Line.receipt_discount, QB's RealPrice; negative on returns), so a $100 item on a 10 %-off
receipt is $90 of sales and its Margin is figured on $90. Cost uses the QB line cost the same way.
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F
from django.utils import timezone

from sales.models import Line, line_amount

CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def cents(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str          # text | date | qty | money | int
    weight: float      # relative PDF width
    optional: bool = False
    total: bool = False


COLUMNS = [
    Column('date', 'Date', 'date', 0.07),
    Column('receipt', 'Receipt #', 'text', 0.06),
    Column('customer', 'Customer', 'text', 0.14, optional=True),
    Column('sku', 'Item #', 'text', 0.11),
    Column('name', 'Item Name', 'text', 0.14, optional=True),
    Column('description', 'Description', 'text', 0.24, optional=True),
    Column('vendor', 'Vendor', 'text', 0.12, optional=True),
    Column('department', 'Department', 'text', 0.10, optional=True),
    Column('cashier', 'Cashier', 'text', 0.07, optional=True),
    Column('quantity', 'Qty', 'qty', 0.045, total=True),
    Column('price', 'Price', 'money', 0.07),
    Column('amount', 'Ext Price', 'money', 0.08, total=True),
    Column('cost', 'Ext Cost', 'money', 0.08, optional=True, total=True),
    Column('margin', 'Margin', 'money', 0.08, optional=True, total=True),
]
BY_KEY = {c.key: c for c in COLUMNS}
DEFAULT_OPTIONAL = {'customer', 'name', 'description'}
GROUPINGS = {'none': 'None (by date)', 'department': 'Department', 'vendor': 'Vendor'}


@dataclass
class Group:
    label: str
    rows: list = field(default_factory=list)
    totals: dict = field(default_factory=lambda: {'quantity': Decimal('0'), 'amount': ZERO, 'cost': ZERO, 'margin': ZERO})

    def add(self, row):
        self.rows.append(row)
        for key in self.totals:
            self.totals[key] += row[key]


@dataclass
class Report:
    period: object
    group_by: str
    columns: list
    groups: list
    receipts: int
    lines: int
    totals: dict
    discounted_lines: int
    highlight_discounts: bool
    include_noninventory: bool

    title = 'General Sales'

    @property
    def subtitle(self):
        parts = [self.period.label]
        if self.group_by != 'none':
            parts.append(f'by {GROUPINGS[self.group_by]}')
        if not self.include_noninventory:
            parts.append('inventory items only')
        return ' · '.join(parts)

    @property
    def grouped(self):
        return self.group_by != 'none'


def selected_columns(optional_keys):
    return [c for c in COLUMNS if not c.optional or c.key in optional_keys]


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    return datetime.combine(start, time.min, tzinfo=tz), datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)


def build(period, optional_keys=DEFAULT_OPTIONAL, group_by='none', highlight_discounts=True, include_noninventory=True):
    lower, upper = _bounds(period.start, period.end)
    lines = (Line.objects
             .filter(document__kind='sale', document__status='completed', document__completed_at__gte=lower,
                     document__completed_at__lt=upper, item__isnull=False)
             .annotate(vendor_name=F('item__primary_vendor__company'), department_name=F('item__department__name'),
                       item_type=F('item__item_type'), completed_at=F('document__completed_at'),
                       source_number=F('document__source_number'), number_value=F('document__number_value'), document_pk=F('document_id'),
                       customer_name=F('document__customer_name'), cashier_name=F('document__cashier'))
             .values('vendor_name', 'department_name', 'item_type', 'completed_at', 'source_number', 'number_value', 'document_pk',
                     'customer_name', 'cashier_name', 'sku', 'item_name', 'description', 'quantity', 'price',
                     'original_price', 'unit_cost', 'position', 'recorded_amount', 'receipt_discount'))
    if not include_noninventory:
        lines = lines.filter(item__item_type='inventory')
    lines = lines.order_by('completed_at', 'document_pk', 'position')

    groups, by_key, receipts = [], {}, set()
    totals = {'quantity': Decimal('0'), 'amount': ZERO, 'cost': ZERO, 'margin': ZERO}
    count = discounted_count = 0
    for r in lines.iterator(chunk_size=5000):
        price = cents(r['price'])  # shown to the cent; the extension uses the exact (up to 4-decimal) price as QB does
        share = cents(r['receipt_discount'])
        amount = line_amount(r['quantity'], r['price'], r['recorded_amount']) - share  # net of the receipt discount share
        unit_cost = cents(r['unit_cost'])
        cost = cents(r['quantity'] * unit_cost)
        discounted = (r['original_price'] is not None and r['price'] < r['original_price']) or share != ZERO
        row = {
            'date': timezone.localtime(r['completed_at']).date(),
            'receipt': str(r['source_number']) if r['source_number'] is not None else (str(r['number_value']) if r['number_value'] is not None else ''),
            'customer': r['customer_name'], 'sku': r['sku'], 'name': r['item_name'], 'description': r['description'],
            'vendor': r['vendor_name'] or '', 'department': r['department_name'] or '', 'cashier': r['cashier_name'] or '',
            'quantity': r['quantity'], 'price': price, 'amount': amount, 'cost': cost, 'margin': amount - cost,
            'discounted': discounted and highlight_discounts,
        }
        if group_by == 'department':
            label = row['department'] or '(No department)'
        elif group_by == 'vendor':
            label = row['vendor'] or '(No vendor)'
        else:
            label = ''
        group = by_key.get(label)
        if group is None:
            group = by_key[label] = Group(label)
            groups.append(group)
        group.add(row)
        for key in totals:
            totals[key] += row[key]
        receipts.add(r['document_pk'])
        count += 1
        discounted_count += discounted
    if group_by != 'none':
        groups.sort(key=lambda g: (g.label.startswith('('), g.label.casefold()))
    return Report(period, group_by, selected_columns(set(optional_keys)), groups, len(receipts), count, totals,
                  discounted_count if highlight_discounts else 0, highlight_discounts, include_noninventory)
