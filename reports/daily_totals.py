# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cash-basis Daily Totals: a shop's ledger view, one row per document, by day.

All timing and tax rules live in `tax_ledger` (shared with the Sales Tax report so the two cannot disagree).
This module only lays the ledger's events out as spreadsheet columns: Received (cash, check, card net of
change — never Account, never gift-certificate redemptions), New Sale, Balance, Tax in Received, the tender columns
and Exempt Received, plus an order's New Sale/Balance once on its creation day. No stored data is changed.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.utils import timezone

from sales.models import Document, OrderPayment, money, tax_amount
from . import tax_ledger
from .tax_ledger import ZERO, Ledger, TENDERS, location, share, tender_key, tenders  # noqa: F401 (re-exported)
from shop_settings.config import tax as shop_tax

POA_NAME = tax_ledger.POA_NAME


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = 'money'
    weight: float = 1
    total: bool = True


COLUMNS = [Column('date', 'Date', 'date', 1.1, False),
           Column('number', 'Number', 'text', 1.5, False),
           Column('customer', 'Customer', 'text', 2, False)] + [
    Column(k, label) for k, label in (
        ('received', 'Received'), ('new_sale', 'New Sale'), ('balance', 'Balance'),
        ('tax', 'Tax in Received'), ('cash', 'Cash'), ('check', 'Check'),
        ('card', 'Card'), ('account', 'Account'), ('gift', 'Gift/Other'),
        ('exempt', 'Exempt Received'))]
DAY_COLUMNS = [COLUMNS[0]] + [next(c for c in COLUMNS if c.key == k) for k in
                             ('received', 'new_sale', 'exempt', 'tax', 'cash', 'check', 'card', 'account', 'gift')]
AMOUNTS = [c.key for c in COLUMNS if c.total]


def totals(rows):
    return {key: sum((r[key] or ZERO for r in rows), ZERO) for key in AMOUNTS}


def build(period):
    lower, upper = tax_ledger.bounds(period.start, period.end)
    walk = tax_ledger.walk(period)
    rows, order_rows = [], defaultdict(list)
    for ev in walk.events:
        doc, order, target = ev.doc, ev.order, ev.target
        day = ev.day
        row = dict.fromkeys(AMOUNTS, ZERO)
        row.update(ev.tender, date=day, number=target.numbered_label, customer=target.customer_name,
                   received=ev.received, tax=ev.tax, exempt=ev.exempt_received,
                   new_sale=None if ev.is_deposit else (ZERO if ev.poa else ev.new_sale),
                   balance=None if ev.is_deposit else (ZERO if ev.poa else money(doc.total or ZERO) - ev.money_in))
        if ev.poa:
            row['customer'] += ' · Payment on account'
        if ev.adjustment:
            row['customer'] += ' · Sales tax adjustment'   # (F3): tax only, no tender
        if ev.excluded == 'unlinked':
            row['customer'] += ' · Unlinked deposit (tax unknown)'
        # Presentation-only split: preserve the existing New Sale and Received arithmetic.
        # Returns are negative merchandise extensions (including exchanges) at what the items really sold for — net
        # of each line's share of the original receipt's discount (Line.net_amount) — with tax on the exact discounted
        # basis (Line.tax_basis), so a $100 item from a 10 %-off receipt returns as 96.30, not 107;
        # give the gross side any residual penny so Gross - Returns = recorded New Sale.
        negative = [line for line in ev.lines if line.amount < 0 and line.item_name != POA_NAME] if not ev.is_deposit else []
        return_value = -(sum((line.net_amount for line in negative), ZERO) + tax_amount(((line.tax_basis, line.tax_rate or ZERO) for line in negative), doc.tax_exempt))
        row['returns_value'] = max(return_value, ZERO)
        rows.append(row)
        if order:
            order_rows[(order.pk, day)].append(row)

    new_orders = list(Document.objects.filter(kind='order', status__in=('open', 'completed'),
                                             created_at__gte=lower, created_at__lt=upper))
    # Historical payments through creation-day end, NOT today's deposit_taken snapshot.
    paid_by_day = defaultdict(list)
    new_ids = [o.pk for o in new_orders]
    for d in Document.objects.filter(deposit_order_id__in=new_ids, status='completed', receipt_type__in=(2, 3),
                                     completed_at__lt=upper).prefetch_related('tenders'):
        amounts, _ = tenders(d)
        paid_by_day[d.deposit_order_id].append((timezone.localdate(d.completed_at), sum(amounts.values(), ZERO)))
    for p in OrderPayment.objects.filter(order_id__in=new_ids, created_at__lt=upper):
        paid_by_day[p.order_id].append((timezone.localdate(p.created_at), money(p.amount)))
    for order in new_orders:
        day = timezone.localdate(order.created_at)
        existing = order_rows[(order.pk, day)]
        if existing:
            row = existing[0]
        else:
            row = dict.fromkeys(AMOUNTS, ZERO)
            row.update(date=day, number=order.numbered_label, customer=order.customer_name)
            rows.append(row)
        row['new_sale'] = money(order.total or ZERO)
        row['balance'] = row['new_sale'] - sum((amt for d, amt in paid_by_day[order.pk] if d <= day), ZERO)
    rows.sort(key=lambda r: r['date'])
    by_date = defaultdict(list)
    for row in rows:
        by_date[row['date']].append(row)
    groups = [SimpleNamespace(label=f'{d:%-m/%-d/%Y}', rows=rs, totals=totals(rs)) for d, rs in sorted(by_date.items())]
    days = []
    day = period.start
    while day <= period.end:
        days.append(dict(date=day, **totals(by_date[day])))
        day += timedelta(days=1)
    grand = totals(rows)
    returns_value = sum((r.get('returns_value', ZERO) for r in rows), ZERO)
    summary = [dict(label='Gross sales (including tax)', amount=grand['new_sale'] + returns_value),
               dict(label='Returns (including tax)', amount=-returns_value),
               dict(label='Net sales (New Sale)', amount=grand['new_sale']),
               dict(label='Refunds paid (net-negative receipts/payments)', amount=sum((min(r['received'], ZERO) for r in rows), ZERO))]
    estimated = walk.estimated_count
    notes = [
        'Gross sales less Returns equals the existing New Sale total, including new orders. Returns includes returned merchandise on exchanges, even when the customer paid a balance. Recorded receipt totals are preserved; any tax residual penny stays on the gross side.',
        'Refunds paid is the negative Received on net-refund receipts and deposit/account-payment refunds. It is already included in Received, is not subtracted from Net sales again, and excludes credits put on Account. Deposits are money movement, not another new sale.',
        'Received is Cash, Check and Card, net of change and refunds — the money taken in that day. Account is not money received; Gift/Other is money that came in when the certificate was sold.',
        'Tax in Received is each receipt’s tax × (Received + Gift/Other) ÷ total, so tax on merchandise bought with a gift certificate counts on the day it is redeemed. Order payments use payment × order tax ÷ order total, worked cumulatively so an order’s payments add up to exactly its tax. Whole cents, rounded half-up as QuickBooks does.',
        'Payments on account pay the customer’s oldest open charges first (receipts and Account-paid order deposits) and release the tax those charges carried; the payment that closes a charge takes its last cent. Exempt Received on a payment is the part that paid Exempt-location charges. Refunds hand back unapplied credit first, then undo prior payments; a return credited to the account and a payment beyond every known charge are credits that fund the customer’s next charge — on that day the credit’s tax is trued up to the charge’s own (and Exempt Received moves with it), no money received. Returned merchandise that was still owed on the account comes off that charge instead (no tax was collected on it, so none is counted or reversed). The Sales Tax report uses this same ledger.',
        'New Sale uses the current recorded receipt/order total, not a history of edits. Cancelled orders are not new sales. An order’s New Sale and Balance appear once on its creation day; Balance subtracts all payments through that day (including Account). Older-order payment rows leave these blank.',
        'Balance on receipts is total less money in, not today’s account balance. Payments on account are not new sales.',
    ]
    if any(row['gift'] for row in rows):
        notes.append("Gift/Other = gift certificate redemptions and other tenders (QuickBooks 'Deposit' tenders on old pickups land here); not counted in Received.")
    if estimated:
        notes.append(f'{estimated} payments on account estimated at {shop_tax().estimate_fraction} — no charge in the data: history before the backup, or a prepayment held for the customer’s next charge (unmatched amounts only: ${walk.estimated_total:,.2f}, tax ${walk.estimated_tax:,.2f}).')
    if walk.reconciled_tax:
        notes.append(f'Credits met the charges they funded (prepayments estimated at {shop_tax().estimate_fraction} when received, returns credited to the account): trued up to the charges’ own tax by ${walk.reconciled_tax:,.2f} (on the charge’s row, no money received).')
    if walk.exempt_estimated_count:
        notes.append(f'{walk.exempt_estimated_count} payments on account with unknown charged history estimated at zero using the customer’s current Exempt location.')
    for key, text in (
        ('unlinked', 'unlinked deposits included in Received; tax unknown and reported as zero'),
        ('missing', 'receipts with missing tender information; unknown amounts not invented'),
        ('derived', 'receipts with one tender amount the backup could not decode, taken as the receipt total less the other tenders'),
        ('payout', 'payout receipts excluded'), ('pickup', 'app pickup receipts excluded (paid on the order)'),
        ('applied', 'legacy deposits excluded (applied and taxed on a pickup receipt)')):
        if walk.excluded[key]:
            notes.append(f'{walk.excluded[key]} {text}.')
    notes.append(f"Spreadsheet check: Received × 0.06542 = ${money(grand['received'] * Decimal('0.06542')):,.2f} (the store's ledger formula); this report: ${grand['tax']:,.2f}")
    report = SimpleNamespace(title='Sales Tax — Daily Totals', subtitle=f'Daily Totals · {period.label}',
                             daily=True, detailed=False, summary=summary, period=period, columns=COLUMNS, day_columns=DAY_COLUMNS,
                             groups=groups, days=days, totals=grand, lines=len(rows), receipts=len(rows),
                             discounted_lines=0, grouped=True, notes=notes, estimated=estimated,
                             walk=walk, empty_message='No documents in this period.')
    report.overview = SimpleNamespace(title='Daily Totals by Day', subtitle=period.label, columns=DAY_COLUMNS,
                                     groups=[SimpleNamespace(label='', rows=days, totals=grand)], totals=grand,
                                     lines=len(days), receipts=len(rows), discounted_lines=0, grouped=False, notes=[])
    return report
