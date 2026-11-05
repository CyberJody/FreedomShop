# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sales by Vendor: receipt lines in a date range, grouped by the vendor of the item sold.

Data rules:
- Sales receipts only (Document.kind='sale', status='completed'); sales orders are not sales.
- Only lines linked to a catalog Item. Lines whose item was deleted in QB POS have no Item and no vendor;
  they are excluded, not shown as an "unknown vendor" group. "Payment On Account" lines have no item either.
- Deposit receipts (receipt_type 2/3) and payouts (4) carry no item lines; they fall out naturally.
- Returns / reversals are imported as negative quantities and stay negative here, so vendor totals are net.
- Vendor comes from Item.primary_vendor. Items with no primary vendor are grouped as "(No vendor)".
- Prices use the saved line price (never the current catalog price). QB POS stores percentage-discounted unit prices
  to four decimals (e.g. 1449.95 less 5% = 1377.4525); the report shows the price to the cent (1377.45) but extends
  with the exact price as QB does: amount = quantity × exact price, rounded half-up, LESS the line's share of a
  receipt-level discount (Line.receipt_discount, QB's RealPrice; negative on returns) — what the line really sold for.
- A line is "sold at a discounted price" when its saved price is below the QB regular price at the time of sale
  (Line.original_price) or it carries a receipt-discount share. Shown in red with a * marker; the footnote explains the marker.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.db.models import F
from django.utils import timezone

from purchasing.models import Vendor
from sales.models import Line, line_amount

NO_VENDOR = '(No vendor)'
CENT = Decimal('0.01')


def cents(value):
    """Drop digits beyond whole cents without rounding."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Row:
    date: date
    receipt: str
    customer: str
    sku: str
    name: str
    description: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    discounted: bool = False


@dataclass
class Group:
    vendor: str
    vendor_pk: int | None
    rows: list = field(default_factory=list)
    quantity: Decimal = Decimal('0')
    amount: Decimal = Decimal('0')


@dataclass
class Report:
    start: date
    end: date
    vendor: Vendor | None
    groups: list
    receipts: int
    lines: int
    quantity: Decimal
    amount: Decimal
    discounted_lines: int = 0

    @property
    def title(self):
        return 'Sales by Vendor'

    @property
    def subtitle(self):
        span = f"{self.start:%-m/%-d/%Y} – {self.end:%-m/%-d/%Y}"
        return f'{self.vendor.company} · {span}' if self.vendor else f'All vendors · {span}'


def default_range(today=None):
    """Current month to date."""
    today = today or timezone.localdate()
    return today.replace(day=1), today


def parse_date(text, fallback):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return fallback


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    lower = datetime.combine(start, time.min, tzinfo=tz)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    return lower, upper


def build(start, end, vendor=None):
    lower, upper = _bounds(start, end)
    lines = (Line.objects
             .filter(document__kind='sale', document__status='completed',
                     document__completed_at__gte=lower, document__completed_at__lt=upper,
                     item__isnull=False)
             .annotate(vendor_name=F('item__primary_vendor__company'), vendor_pk=F('item__primary_vendor_id'),
                       completed_at=F('document__completed_at'), source_number=F('document__source_number'), number_value=F('document__number_value'),
                       document_pk=F('document_id'), customer_name=F('document__customer_name'))
             .values('vendor_name', 'vendor_pk', 'completed_at', 'source_number', 'number_value', 'document_pk', 'customer_name',
                     'sku', 'item_name', 'description', 'quantity', 'price', 'original_price', 'recorded_amount', 'receipt_discount'))
    if vendor is not None:
        lines = lines.filter(item__primary_vendor=vendor)
    lines = lines.order_by('vendor_name', 'completed_at', 'document_pk', 'position')

    groups, by_key, receipts = [], {}, set()
    total_qty = total_amt = Decimal('0')
    count = discounted_count = 0
    for r in lines:
        key = r['vendor_pk']
        group = by_key.get(key)
        if group is None:
            group = by_key[key] = Group(vendor=r['vendor_name'] or NO_VENDOR, vendor_pk=key)
            groups.append(group)
        when = timezone.localtime(r['completed_at']).date()
        number = str(r['source_number']) if r['source_number'] is not None else (str(r['number_value']) if r['number_value'] is not None else '')
        price = cents(r['price'])  # shown to the cent; the extension uses the exact (up to 4-decimal) price as QB does
        share = cents(r['receipt_discount'])
        amount = line_amount(r['quantity'], r['price'], r['recorded_amount']) - share  # net of the receipt discount share
        discounted = (r['original_price'] is not None and r['price'] < r['original_price']) or bool(share)
        group.rows.append(Row(when, number, r['customer_name'], r['sku'], r['item_name'], r['description'],
                              r['quantity'], price, amount, discounted))
        discounted_count += discounted
        group.quantity += r['quantity']
        group.amount += amount
        total_qty += r['quantity']
        total_amt += amount
        receipts.add(r['document_pk'])
        count += 1
    # "(No vendor)" sorts last regardless of database NULL ordering.
    groups.sort(key=lambda g: (g.vendor_pk is None, g.vendor.casefold()))
    return Report(start, end, vendor, groups, len(receipts), count, total_qty, total_amt, discounted_count)


# ---- Summary views -------
# QB POS Vendor Sales Summary: one line per vendor (Qty Sold, Ext Price, Ext Cost, Margin %), drilling down to one
# line per item (Qty Sold, Item Name, Item Description). Same line rules as the detail above; cost is the QB/app line
# cost at the time of sale (Line.unit_cost), truncated to cents the same way as price.

from .general_sales import Column  # noqa: E402

VIEWS = {'items': 'Summary by item', 'vendors': 'Vendor totals', 'detail': 'Detail (every receipt line)'}
PCT = Decimal('0.01')
ZERO = Decimal('0.00')

ITEM_COLUMNS = [
    Column('name', 'Item Name', 'text', 0.20),
    Column('description', 'Item Description', 'text', 0.40),
    Column('quantity', 'Qty Sold', 'qty', 0.08, total=True),
    Column('amount', 'Ext Price', 'money', 0.11, total=True),
    Column('cost', 'Ext Cost', 'money', 0.11, total=True),
    Column('margin_pct', 'Margin %', 'percent', 0.08, total=True),
]
VENDOR_COLUMNS = [
    Column('vendor', 'Vendor', 'text', 0.42),
    Column('quantity', 'Qty Sold', 'qty', 0.12, total=True),
    Column('amount', 'Ext Price', 'money', 0.16, total=True),
    Column('cost', 'Ext Cost', 'money', 0.16, total=True),
    Column('margin_pct', 'Margin %', 'percent', 0.12, total=True),
]


def margin_pct(amount, cost):
    """(Ext Price − Ext Cost) / Ext Price as a percent, truncated to 2 places; None when nothing was sold."""
    return ((amount - cost) * 100 / amount).quantize(PCT, rounding=ROUND_DOWN) if amount else None


def _totals():
    return {'quantity': Decimal('0'), 'amount': ZERO, 'cost': ZERO, 'margin_pct': None}


def _add(totals, quantity, amount, cost):
    totals['quantity'] += quantity
    totals['amount'] += amount
    totals['cost'] += cost
    totals['margin_pct'] = margin_pct(totals['amount'], totals['cost'])


@dataclass
class SummaryGroup:
    label: str
    vendor_pk: int | None = None
    rows: list = field(default_factory=list)
    totals: dict = field(default_factory=_totals)


@dataclass
class SummaryReport:
    start: date
    end: date
    vendor: Vendor | None
    view: str
    groups: list
    totals: dict
    receipts: int
    lines: int

    title = 'Sales by Vendor'
    discounted_lines = 0
    empty_message = 'No item sales in this range.'

    @property
    def columns(self):
        return VENDOR_COLUMNS if self.view == 'vendors' else ITEM_COLUMNS

    @property
    def grouped(self):
        return self.view == 'items'

    @property
    def subtitle(self):
        span = f"{self.start:%-m/%-d/%Y} – {self.end:%-m/%-d/%Y}"
        who = self.vendor.company if self.vendor else 'All vendors'
        return f'{who} · {span} · {VIEWS[self.view]}'

    @property
    def notes(self):
        items = sum(len(g.rows) for g in self.groups) if self.view == 'items' else None
        parts = [f'{self.receipts} receipts', f'{self.lines} lines']
        if items is not None:
            parts.insert(0, f'{items} items')
        return [' · '.join(parts) + ' · Margin % = (Ext Price − Ext Cost) ÷ Ext Price']


def summarize(start, end, vendor=None, view='items'):
    view = view if view in ('items', 'vendors') else 'items'
    lower, upper = _bounds(start, end)
    lines = (Line.objects
             .filter(document__kind='sale', document__status='completed',
                     document__completed_at__gte=lower, document__completed_at__lt=upper, item__isnull=False)
             .values_list('item__primary_vendor_id', 'item__primary_vendor__company', 'item_id', 'item__name',
                          'item__sku', 'item__description', 'quantity', 'price', 'unit_cost', 'document_id', 'recorded_amount', 'receipt_discount'))
    if vendor is not None:
        lines = lines.filter(item__primary_vendor=vendor)
    groups, items, receipts = {}, {}, set()
    totals = _totals()
    count = 0
    for vendor_pk, vendor_name, item_pk, name, sku, description, qty, price, unit_cost, doc_pk, recorded, share in lines.iterator(chunk_size=5000):
        amount = line_amount(qty, price, recorded) - cents(share)  # net of the receipt discount share
        cost = cents(qty * cents(unit_cost or ZERO))
        group = groups.get(vendor_pk)
        if group is None:
            group = groups[vendor_pk] = SummaryGroup(vendor_name or NO_VENDOR, vendor_pk)
        _add(group.totals, qty, amount, cost)
        if view == 'items':
            row = items.get(item_pk)
            if row is None:
                row = items[item_pk] = dict(name=name or sku, description=description, quantity=Decimal('0'), amount=ZERO,
                                            cost=ZERO, margin_pct=None, item_pk=item_pk)
                group.rows.append(row)
            _add(row, qty, amount, cost)
        _add(totals, qty, amount, cost)
        receipts.add(doc_pk)
        count += 1
    ordered = sorted(groups.values(), key=lambda g: (g.vendor_pk is None, g.label.casefold()))
    if view == 'items':
        for g in ordered:
            g.rows.sort(key=lambda r: (r['name'] or '').casefold())
        out = ordered
    else:
        out = [SummaryGroup('All vendors', rows=[dict(vendor=g.label, vendor_pk=g.vendor_pk, **g.totals) for g in ordered], totals=totals)]
    return SummaryReport(start, end, vendor, view, out, totals, len(receipts), count)
