# SPDX-License-Identifier: AGPL-3.0-or-later
"""Customer charge accounts: charges and payments on account on receipts made in this app.

QB POS flow: the Account tender opens "Charge Account" with "Charge to account" (the account
tender amount raises the balance) or "Make payment on account" (adds a "Payment On Account" line, no tax, paid with any
other tender, which lowers the balance). The receipt prints a signature block when charged and Previous Account
Balance / Account Balance.

Sales orders: an order payment (deposit) "Paid by" Account charges that amount to the customer's account (a negative
amount, i.e. a refund to Account, credits it back). A payment on account taken while working on a sales order is a
Payment On Account receipt opened from the order, so it prints with the balances like any other.

Balance = Customer.imported_account_balance (QB CreditUsed at the backup) + Customer.app_account_activity, where the
activity is recomputed from the customer's completed native receipts and native order payments (never adjusted
incrementally), so edits, corrections and cancellations always reconcile.
"""
from config.provenance import native_source
from decimal import Decimal

from customers.models import Customer

from .models import Document, OrderPayment

POA_NAME = 'Payment On Account'
ZERO = Decimal('0.00')
SIGNATURE_TEXT = 'I agree to pay above amount according to card issuer agreement (merchant agreement if credit voucher).'


def charged(doc):
    """Amount put on the customer's account by this receipt's Account tender(s)."""
    return sum((Decimal(str(e.get('amount') or 0)) for e in (doc.payment_breakdown or []) if e.get('method') == 'account'), ZERO)


def paid_on_account(doc):
    """Payment On Account lines on this receipt (they lower the balance)."""
    return sum((line.amount for line in doc.lines.all() if line.item_id is None and line.item_name == POA_NAME), ZERO)


def effect(doc):
    """Change in the customer's balance from one native completed receipt."""
    if doc.kind != 'sale' or doc.status != 'completed' or doc.imported:
        return ZERO
    return charged(doc) - paid_on_account(doc)


def _native_receipts(customer_id):
    return (Document.objects.filter(native_source(), customer_id=customer_id, kind='sale', status='completed')
            .prefetch_related('lines').order_by('completed_at', 'pk'))


def _order_charges(customer_id):
    # Every order payment made in this app, on app-made and on imported (QuickBooks) open orders alike;
    # QuickBooks' own deposits are receipts, never OrderPayments, so nothing is counted twice.
    return (OrderPayment.objects.filter(order__customer_id=customer_id, order__kind='order', method='account')
            .order_by('created_at', 'pk'))


def ledger(customer_id):
    """Account activity in this app, oldest first: (when, ('sale'|'order_payment', pk), amount)."""
    entries = [(doc.completed_at, ('sale', doc.pk), effect(doc)) for doc in _native_receipts(customer_id)]
    entries += [(p.created_at, ('order_payment', p.pk), p.amount) for p in _order_charges(customer_id)]
    return sorted((e for e in entries if e[2]), key=lambda e: (e[0], e[1]))


def refresh(*customer_ids):
    """Recompute app_account_activity for these customers. Call inside the saving transaction."""
    for customer_id in {c for c in customer_ids if c}:
        total = sum((amount for _, _, amount in ledger(customer_id)), ZERO)
        Customer.objects.filter(pk=customer_id).update(app_account_activity=total)


def balances(doc):
    """(previous, new) account balance for a native receipt, as of when it was completed; None if it has no customer."""
    if not doc.customer_id:
        return None
    previous = doc.customer.imported_account_balance
    for when, key, amount in ledger(doc.customer_id):
        if key == ('sale', doc.pk) or (doc.completed_at and when > doc.completed_at):
            break
        previous += amount
    return previous, previous + effect(doc)


def order_charged(order):
    """Order payments charged to the customer's account (net of refunds to Account)."""
    return sum((p.amount for p in order.payments.all() if p.method == 'account'), ZERO)


def shows_account(doc):
    """Receipts print account lines when they charge to or pay on the customer's account."""
    return not doc.imported and doc.kind == 'sale' and doc.status == 'completed' and bool(charged(doc) or paid_on_account(doc))
