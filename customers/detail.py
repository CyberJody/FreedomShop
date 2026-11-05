# SPDX-License-Identifier: AGPL-3.0-or-later
"""Customer Detail panel data, laid out like QB POS: Contact Info, Notes, Customer Settings, QuickBooks Options,
Statistics, History (Recent 100) and Addresses.

Statistics are computed from the receipts and orders in this app (imported QB history plus anything entered here):
- Sales = total of completed sales receipts with a positive total (receipt type Sales); Returns = completed sales
  receipts with a negative total plus sales reversals; Discounts = receipt-level discounts on those receipts.
  Deposits, deposit reversals and payouts are not sales.
- Average Receipt = Sales / number of positive sales receipts.
- Orders: On Order = total of open sales orders, Deposits = paid on them, Balance Due = On Order - Deposits.
QB POS's own Statistics may differ slightly (it keeps running totals and a star rating that are not in the backup).
"""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from sales import accounts
from sales.models import Document, Line

ZERO = Decimal('0.00')
CENT = Decimal('0.01')
HISTORY_LIMIT = 100


def cents(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def statistics(customer):
    receipts = Document.objects.filter(customer=customer, kind='sale', status='completed')
    sales = returns = discounts = ZERO
    count = 0
    # Payments on account are not sales (QB counts only merchandise); take them out of the receipt totals.
    poa = {}
    for doc_id, qty, price in Line.objects.filter(document__in=receipts, item__isnull=True, item_name=accounts.POA_NAME).values_list('document_id', 'quantity', 'price'):
        poa[doc_id] = poa.get(doc_id, ZERO) + cents(qty * price)
    last = None
    for pk, receipt_type, total, discount, when in receipts.values_list('pk', 'receipt_type', 'total', 'discount', 'completed_at'):
        if receipt_type not in (None, 0, 1) or total is None:
            continue  # deposits, deposit reversals, payouts
        total -= poa.get(pk, ZERO)
        if not total:
            continue  # payment on account only
        if receipt_type == 1 or total < 0:
            returns += -total if total < 0 else total
        else:
            sales += total
            count += 1
            if when and (last is None or when > last):
                last = when
            discounts += discount or ZERO
    orders = list(Document.objects.filter(customer=customer, kind='order', status='open').prefetch_related('payments'))
    on_order = sum((o.total or ZERO for o in orders), ZERO)
    deposits = sum((o.paid for o in orders), ZERO)
    return dict(last_sale=timezone.localdate(last) if last else customer.imported_last_sale, sales=sales, returns=returns,
                discounts=discounts, receipts=count, average=cents(sales / count) if count else ZERO,
                on_order=on_order, deposits=deposits, balance_due=on_order - deposits, open_orders=len(orders))


def history(customer, limit=HISTORY_LIMIT):
    """Most recent receipts and orders, newest first, with account charges and payments called out."""
    docs = (Document.objects.filter(customer=customer).exclude(kind='sale', status='held')
            .prefetch_related('tenders', 'lines', 'payments').order_by('-completed_at', '-created_at', '-pk')[:limit])
    rows = []
    for doc in docs:
        if doc.kind == 'order':
            label = 'Sales Order'
        elif doc.receipt_type == 2:
            label = 'Deposit'
        elif doc.receipt_type == 3:
            label = 'Deposit Reversal'
        elif doc.receipt_type == 4:
            label = 'Payout'
        elif doc.receipt_type == 1 or (doc.total or ZERO) < 0:
            label = 'Return'
        else:
            label = 'Sales Receipt'
        notes = []
        if doc.status == 'cancelled':
            notes.append('Cancelled')
        # Review finding 7: an order's account activity is its app order payments (QuickBooks orders carry no
        # tenders), whether it is open or closed; only receipts from QuickBooks read their QB tenders.
        if doc.kind == 'order':
            net = accounts.order_charged(doc)
            charged, paid = max(net, ZERO), ZERO
            if net < 0:
                notes.append(f'Credited to Account ${-net:,.2f}')
        elif doc.imported:
            charged = sum((t.amount for t in doc.tenders.all() if t.method == 'account' and t.amount and not t.use_in_total), ZERO)
            paid = -sum((t.amount for t in doc.tenders.all() if t.method == 'account' and t.amount and t.use_in_total), ZERO)
        elif doc.kind == 'sale' and doc.status == 'completed':
            charged, paid = accounts.charged(doc), accounts.paid_on_account(doc)
        else:
            charged = paid = ZERO
        if charged > 0:
            notes.append(f'Charged to Account ${charged:,.2f}')
        if paid > 0:
            notes.append(f'Payment On Account ${paid:,.2f}')
        rows.append(dict(date=doc.completed_at or doc.created_at, label=label, number=doc.number, total=doc.total,
                         notes=notes, url=f'/pos/{doc.kind}/{doc.token}/'))
    return rows


OPEN_CHARGES_LIMIT = 100


def open_charges(customer, limit=OPEN_CHARGES_LIMIT):
    """what the customer still owes, charge by charge (receipt or Account-paid order payment), from the shared
    tax ledger's FIFO state — payments on account clear the oldest charges first. Newest first, capped for the two
    house accounts with thousands of open lots. `total` and `count` cover the whole list."""
    from reports.tax_ledger import open_charges as ledger_open_charges
    from sales.models import OrderPayment
    lots = ledger_open_charges(customer)
    if not lots:
        return dict(rows=[], count=0, total=ZERO, hidden=0)
    sale_pks = [key[1] for key, _, _ in lots if key[0] == 'sale']
    payment_pks = [key[1] for key, _, _ in lots if key[0] == 'order_payment']
    docs = Document.objects.in_bulk(sale_pks)
    payments = {p.pk: p for p in OrderPayment.objects.filter(pk__in=payment_pks).select_related('order')}
    rows = []
    for key, charged, owed in lots:
        if key[0] == 'sale':
            doc = docs.get(key[1])
            if doc is None:
                continue
            label = 'Deposit' if doc.receipt_type in (2, 3) else 'Sales Receipt'
            rows.append(dict(date=doc.completed_at, label=label, number=doc.number, charged=charged, owed=owed,
                             url=f'/pos/sale/{doc.token}/'))
        else:
            payment = payments.get(key[1])
            if payment is None:
                continue
            rows.append(dict(date=payment.created_at, label='Sales Order payment', number=payment.order.number, charged=charged, owed=owed,
                             url=f'/pos/order/{payment.order.token}/'))
    rows.sort(key=lambda r: (r['date'] or timezone.now()), reverse=True)
    total = sum((r['owed'] for r in rows), ZERO)
    return dict(rows=rows[:limit], count=len(rows), total=total, hidden=max(len(rows) - limit, 0))


def addresses(customer):
    bill_to = '\n'.join(v for v in [customer.display_name, *[l for l in (customer.street or '').splitlines() if l.strip()],
                                    ' '.join(filter(None, [f'{customer.city},' if customer.city else '', customer.state, customer.postal_code]))] if v)
    saved = list(customer.addresses.all())
    options = [dict(label='Bill To Address', detail=bill_to, default=not any(a.is_default for a in saved) and not customer.do_not_mail)]
    options += [dict(label=a.label or 'Shipping', detail=a.address, default=a.is_default) for a in saved]
    return options
