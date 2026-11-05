# SPDX-License-Identifier: AGPL-3.0-or-later
"""the Awaiting Pickup screen (Navigator > Point of Sale, under Sales History).

Two lists on one page:

1. Paid sales awaiting pickup: the receipts held for pickup and not yet marked picked up (`Document.awaiting_pickup`).
2. Sales orders ready for pickup: open customer orders with "Hold for Pickup" ticked
   (`Document.order_pickup`; staff untick it when a third party delivers) and at least one line whose goods were
   RECEIVED IN THIS APP: on a receiving voucher made here (not brought over from QuickBooks) and posted on or after
   go-live (`GO_LIVE`). That rule keeps the QuickBooks backlog off the page, so there is no date filter. Nothing records
   "ready" on an order line, so it is worked out from the facts the app already keeps:

   * what is still to be picked up on a line: ordered - picked up (`Line.fulfilled`, the order's pickup receipts);
   * a line linked to a PO line (Generate PO / Create PO from Selected Orders): what those PO lines received, capped
     by what app vouchers received on them;
   * a special-order piece with no link (every QB-era order; "SOLD" pieces, see `purchasing.order_coverage`): the
     received quantity of the non-cancelled POs for that piece, plus any voucher that received it without a PO, shared
     among the open orders for the piece oldest order first (the same matching the PO screens use). The app vouchers'
     share is the newest part of that pool, so an order counts only what its share holds of app-received goods.
   * stock lines (goods on the shelf, never received for the order) don't count, nor do QB-received goods.

   "Came in" is the latest app receiving voucher for those lines.
"""
from config.provenance import imported_source
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from . import accounts
from .models import Document, Line, quantity_text

ZERO = Decimal('0')
GO_LIVE = date(2026, 10, 21)   # a shop's first day on this app (cutover load 10/21/2026)


def short_items(lines, limit=3):
    """'2 × Seely counter stool, 1 × Bench' (descriptions shortened; '+ 2 more' past `limit`)."""
    parts = []
    for qty, line in lines:
        name = ' '.join((line.description or line.item_name or line.sku or 'Item').split()) or 'Item'
        if len(name) > 40:
            name = name[:39].rstrip() + '…'
        parts.append(f'{quantity_text(qty)} × {name}')
    if len(parts) > limit:
        return ', '.join(parts[:limit]) + f' + {len(parts) - limit} more'
    return ', '.join(parts)


def phone_of(doc):
    customer = doc.customer
    if customer is None:
        return ''
    numbers = customer.phone_numbers()
    return numbers[0] if numbers else ''


def short_note(text, limit=60):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def goods_lines(doc):
    """The lines a customer collects (no payment on account, gift certificate, restocking fee or return)."""
    return [l for l in doc.lines.all() if l.quantity > 0 and not l.restocking_fee and not l.certificate_number
            and not (l.item_id is None and l.item_name == accounts.POA_NAME)]


# --- 1. Paid sales awaiting pickup ---------------------------------------------------------------------------

def awaiting_receipts(today=None):
    """Rows for receipts held for pickup and not yet picked up: est. pickup date first (no date last), then sale date."""
    today = today or timezone.localdate()
    docs = (Document.objects.filter(kind='sale', status='completed', hold_for_pickup=True, picked_up_at__isnull=True)
            .select_related('customer').prefetch_related('lines'))
    rows = []
    for doc in docs:
        sold = timezone.localdate(doc.completed_at) if doc.completed_at else timezone.localdate(doc.created_at)
        rows.append(dict(doc=doc, number=doc.number_or_label, sold=sold, customer=doc.customer_name, phone=phone_of(doc),
                         items=short_items([(l.quantity, l) for l in goods_lines(doc)]), est=doc.est_pickup_date,
                         days=(today - sold).days, cashier=doc.cashier, notes=short_note(doc.notes)))
    rows.sort(key=lambda r: (r['est'] is None, r['est'] or today, r['sold'], r['doc'].pk))
    return rows


# --- 2. Sales orders ready for pickup -------------------------------------------------------------------------------

@dataclass
class LineReady:
    remaining: Decimal                    # still to be picked up (ordered - picked up)
    ready: Decimal = ZERO                 # of that, received in this app and in the store now
    came_in: object = None                # latest app receiving voucher day for those goods
    how: str = ''                         # 'received' when ready


@dataclass
class OrderReady:
    order: Document
    lines: list = field(default_factory=list)   # [(Line, LineReady)]


def _voucher_day(doc):
    when = doc.posted_at or doc.created_at
    return timezone.localdate(when) if when else None


def app_voucher(doc):
    """A receiving voucher made in this app (not imported from QuickBooks) and posted on or after go-live."""
    day = _voucher_day(doc)
    return not imported_source(doc.source_id) and day is not None and day >= GO_LIVE


def line_readiness(order_lines):
    """{Line pk: LineReady} for the given open customer-order lines (Line objects with `document` loaded and their
    pickups prefetched as `deliveries`). Every open order line for the same piece takes part in the oldest-first
    sharing, so pass all of them (ready_orders does, ticked or not)."""
    from purchasing.models import PurchaseLine
    from purchasing.order_coverage import special_item_ids, sold_marked

    def picked(line):
        return sum((d.quantity for d in line.deliveries.all() if d.document.status == 'completed'), ZERO)

    def latest(days):
        days = [d for d in days if d]
        return max(days) if days else None

    lines = [l for l in order_lines if l.quantity > 0 and not l.restocking_fee]
    result = {}
    fulfilled = {}
    for line in lines:
        fulfilled[line.pk] = picked(line)
        result[line.pk] = LineReady(max(line.quantity - fulfilled[line.pk], ZERO))

    # Lines linked to PO lines.
    linked = defaultdict(list)
    for p in PurchaseLine.objects.filter(sales_line__in=[l.pk for l in lines], document__kind='po').exclude(
            document__status='cancelled'):
        linked[p.sales_line_id].append(p)
    po_line_ids = [p.pk for ps in linked.values() for p in ps]

    # Unlinked lines: special-order pieces by item (or QB item number when QB deleted the item). Stock lines: none.
    unlinked = [l for l in lines if l.pk not in linked]
    specials = special_item_ids([l.item for l in unlinked if l.item_id])
    special_keys = defaultdict(list)
    for line in unlinked:
        if line.item_id and line.item_id in specials:
            special_keys[('item', line.item_id)].append(line)
        elif not line.item_id and line.sku and line.document.from_quickbooks and sold_marked(line.item_name, line.description):
            special_keys[('sku', line.sku)].append(line)

    item_keys = {k[1] for k in special_keys if k[0] == 'item'}
    sku_keys = {k[1] for k in special_keys if k[0] == 'sku'}
    key_po_lines = defaultdict(list)
    key_direct = defaultdict(list)      # vouchers that received the piece without a PO
    if item_keys or sku_keys:
        match = Q(item_id__in=item_keys) | Q(item__isnull=True, sku__in=sku_keys)
        for p in PurchaseLine.objects.filter(match, document__kind='po', sales_line__isnull=True).exclude(
                document__status='cancelled'):
            key = ('item', p.item_id) if p.item_id else ('sku', p.sku)
            key_po_lines[key].append(p)
            po_line_ids.append(p.pk)
        for v in PurchaseLine.objects.filter(match, document__kind='voucher', document__status='posted',
                                             source_line__isnull=True).select_related('document'):
            key = ('item', v.item_id) if v.item_id else ('sku', v.sku)
            key_direct[key].append(v)
    app_received = defaultdict(lambda: ZERO)   # PO line pk -> qty received on app vouchers
    app_days = defaultdict(list)               # PO line pk -> app voucher days
    for v in PurchaseLine.objects.filter(source_line__in=po_line_ids, document__kind='voucher',
                                         document__status='posted').select_related('document'):
        if app_voucher(v.document):
            app_received[v.source_line_id] += v.quantity
            app_days[v.source_line_id].append(_voucher_day(v.document))

    for line in lines:
        if line.pk not in linked:
            continue
        info = result[line.pk]
        received = sum((p.received for p in linked[line.pk]), ZERO)
        in_store = min(max(min(received, line.quantity) - fulfilled[line.pk], ZERO), info.remaining)
        info.ready = min(in_store, sum((app_received[p.pk] for p in linked[line.pk]), ZERO))
        if info.ready:
            info.how = 'received'
            info.came_in = latest(d for p in linked[line.pk] for d in app_days[p.pk])

    for key, wanted in special_keys.items():
        direct = key_direct[key]
        pool = sum((p.received for p in key_po_lines[key]), ZERO) + sum((v.quantity for v in direct), ZERO)
        app_direct = [v for v in direct if app_voucher(v.document)]
        app_pool = (sum((app_received[p.pk] for p in key_po_lines[key]), ZERO)
                    + sum((v.quantity for v in app_direct), ZERO))
        app_from = max(pool - app_pool, ZERO)     # app-received goods are the newest part of the pool
        days = latest([d for p in key_po_lines[key] for d in app_days[p.pk]] + [_voucher_day(v.document) for v in app_direct])
        start = ZERO
        for line in sorted(wanted, key=lambda l: (l.document.created_at, l.document_id, l.pk)):
            take = min(max(pool - start, ZERO), line.quantity)
            app_take = max(min(start + take, pool) - max(start, app_from), ZERO)
            start += take
            info = result[line.pk]
            in_store = min(max(take - fulfilled[line.pk], ZERO), info.remaining)
            info.ready = min(in_store, app_take)
            if info.ready:
                info.how, info.came_in = 'received', days
    return result


def ready_orders(today=None):
    """Rows for open sales orders with Hold for Pickup ticked and something received in the app ready to pick up,
    oldest 'came in' first."""
    today = today or timezone.localdate()
    orders = list(Document.objects.filter(kind='order', status='open').select_related('customer')
                  .prefetch_related('payments', 'lines__deliveries__document', 'lines__item__department'))
    lines = []
    for order in orders:
        for line in order.lines.all():
            line.document = order       # the order already loaded (dates, source)
            lines.append(line)
    readiness = line_readiness(lines)   # every open order shares the pieces, ticked or not
    rows = []
    for order in orders:
        if not order.order_pickup:
            continue
        parts = [(line, readiness[line.pk]) for line in order.lines.all() if line.pk in readiness]
        open_parts = [(l, r) for l, r in parts if r.remaining > 0]
        ready = [(l, r) for l, r in open_parts if r.ready > 0]
        if not ready:
            continue
        came_in = max((r.came_in for _, r in ready if r.came_in), default=None)
        rows.append(dict(doc=order, number=order.number_or_label, ordered=timezone.localdate(order.created_at),
                         customer=order.customer_name, phone=phone_of(order),
                         ready_qty=sum((r.ready for _, r in ready), ZERO),
                         open_qty=sum((r.remaining for _, r in open_parts), ZERO),
                         items=short_items([(r.ready, l) for l, r in ready]), came_in=came_in,
                         days=(today - came_in).days if came_in else None,
                         balance=order.balance_due, paid=order.paid))
    rows.sort(key=lambda r: (r['came_in'] is None, r['came_in'] or today, r['ordered'], r['doc'].pk))
    for row in rows:
        row['ready'] = f"{quantity_text(row['ready_qty'])} of {quantity_text(row['open_qty'])}"
    return rows
