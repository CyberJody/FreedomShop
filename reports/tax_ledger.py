# SPDX-License-Identifier: AGPL-3.0-or-later
"""One cash-basis ledger behind Sales Tax and Daily Totals.

Tax counts on money received, on the day it is received:
- Sales receipt: the receipt's tax × money in ÷ receipt total on the sale day. "Money in" is every tender except
  Account (cash, check, card net of change, gift-certificate redemptions). The Account part is a charge on the
  customer's ledger carrying the rest of the receipt's tax (deferred until paid).
- Payment on account: taxed when received. The payment pays the customer's oldest open charges first (FIFO) and
  releases their deferred tax; partial payments take their share of a charge's tax, the payment that closes a charge
  takes its last cent. Each part of the payment keeps the paid charge's tax location and taxable / non-taxable bases,
  so the reports list it where the charge belonged. Debt QuickBooks knows but the data has no receipt for (an opening
  balance from before the backup) is the oldest lot, estimated at exactly r/(100 + r) of it — r the default tax
  location's rate (Tax Settings), 7/107 at 7 % — for a taxable customer and zero for an exempt one, and flagged; credit QuickBooks held at the backup funds the first charges silently (its money moved,
  and was taxed, before the data). A payment beyond every known charge is a prepayment: taxed now at the same estimate,
  flagged, and held as credit — the customer's next charge is funded from it and, on the charge's day, the estimate
  is replaced by that charge's own tax, bases and location (a "tax trued up" row per location, no money moved). A
  return credited to the account first comes off the charge it was sold on if that charge is still owed (no tax was
  collected on it, so none is counted or reversed — the deferred obligation just shrinks); the rest is a credit the
  same way, carrying the returned merchandise's own collected tax and location until it funds a charge (trued up to
  what it really bought) or is paid back. A refund hands back unapplied credit first, then undoes the newest
  payments (reopening their charges); a refund beyond every payment in the data paid back money received before
  the backup, so its debt reopens as the oldest lot at the same estimate.
- QuickBooks' balance at the backup is the authority on what is owed, not on when: debt above the imported receipts
  is seeded as the oldest lot; a balance below them (an adjustment QuickBooks made with no receipt in the data) is
  applied at the backup moment — after the customer's last imported receipt — first clearing what imported charges
  still show as owed (no money received, no tax), and only what is left beyond them is credit QuickBooks holds for
  the customer, funding later charges silently (money taxed before the data). An unexplained difference never changes
  which charge a real payment inside the data paid.
- Customer order deposit / payment: order tax × payment ÷ order total on the payment's day, computed cumulatively
  so an order's payments sum exactly to the order's tax (the payment that completes the order carries the remainder).
  each payment's figures are FIXED on the day the money is received (stored on the deposit receipt
  or OrderPayment, `OrderPaymentTaxLock`; `lock_payment`, `lock_order_payment_tax`). An edit after that (a chair
  added, a price, Tax-exempt) is never refused and changes only the order's total, tax and balance due; the tax
  difference is booked on the NEXT payment, on its own day, computed from the order as it stands then — the payment
  that completes the order carries the remainder, so the order's payments add up to its final tax to the cent, and a
  refund that empties the order reverses exactly what was booked. Payments not yet locked fall back to the order as
  it stands now. A deposit paid by Account is a charge (its tax share deferred). Legacy deposits applied on a
  2008–09 pickup receipt, deposits with no order, payouts and app pickup receipts paid on the order are excluded as
  before.
- Gift-certificate redemptions are not money received (the cash came in when the certificate was sold — QB's
  "Total Available for Deposit" rule), so Daily Totals keeps them out of Received; the tax on merchandise bought with
  a certificate still counts on the redemption day so it is never lost (the certificate's own sale carries no tax).

All amounts are whole cents rounded half-up (QB rule). The replay writes nothing: the ledger is replayed from the
documents every time, so Sales Tax, Daily Totals and the "On account · $owed" status can't disagree. The only
writes are the order-payment tax locks (`lock_payment` inside the payment's own transaction, `lock_order` /
`lock_all_orders` for the backfill), which store what the replay reports and never overwrite a stored value.
"""
from config.provenance import native_source
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import F, Prefetch, Q
from django.utils import timezone

from sales.models import Document, Line, OrderPayment, Tender, TaxLock, money
from shop_settings.config import tax as shop_tax

ZERO = Decimal('0.00')
TENDERS = ('cash', 'check', 'card', 'account', 'gift')
MONEY_IN = ('cash', 'check', 'card')
POA_NAME = 'Payment On Account'
LINE_FIELDS = ('document_id', 'item_name', 'quantity', 'price', 'recorded_amount', 'tax_rate', 'receipt_discount',
               'returned_from_receipt', 'returned_from_line')


def taxed_location():
    """Report section for taxable money with no other home: the default tax location's name (Tax Settings)."""
    return shop_tax().default_location.name


def exempt_location():
    """Report section for exempt money: the exempt tax location's name (Tax Settings)."""
    return shop_tax().exempt_location.name


def is_exempt_location(name):
    loc = shop_tax().location(name)
    return bool(loc and loc.is_exempt)


def __getattr__(name):
    # Compatibility names for the default and exempt sections; existing callers
    # and tests read them as module constants.
    if name == 'DEFAULT_LOCATION':
        return taxed_location()
    if name == 'EXEMPT':
        return exempt_location()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def bounds(start, end):
    tz = timezone.get_current_timezone()
    return datetime.combine(start, time.min, tzinfo=tz), datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)


def location(doc):
    return shop_tax().section_of(doc.tax_location, doc.tax_exempt)


def share(amount, tax, total):
    return money(amount * (tax or ZERO) / total) if total else ZERO


def cumulative_order_tax(paid, order_tax, order_total):
    """Tax counted on an order's payments so far (`paid`, cumulative): its share of the order's tax, all of it once
    the order is paid in full (CAP: money beyond the total carries none). `replay` books each payment's difference."""
    return order_tax if order_total and paid >= order_total else share(paid, order_tax, order_total)


def counted_order_payments(order):
    """The payment rows the ledger counts for `order`, oldest first: its QuickBooks deposit / deposit-refund receipts
    (none when QuickBooks already used the deposit on a pickup there — `_parse` skips those) and the payments or
    refunds recorded in this app, each as (when, kind, pk, row, money_in, account). `kind` 0 = Document, 1 =
    OrderPayment, the same sort key `replay` uses."""
    rows = []
    if not (order.deposit_used or ZERO) > 0:
        for doc in (Document.objects.filter(deposit_order=order, kind='sale', status='completed', receipt_type__in=(2, 3))
                    .prefetch_related('tenders')):
            tender, _ = tenders(doc)
            rows.append((doc.completed_at, 0, doc.pk, doc, sum((tender[k] for k in MONEY_IN), ZERO) + tender['gift'], tender['account']))
    for payment in OrderPayment.objects.filter(order=order):
        key = tender_key(payment.method)
        amount = money(payment.amount)
        rows.append((payment.created_at, 1, payment.pk, payment, amount if key != 'account' else ZERO, amount if key == 'account' else ZERO))
    return sorted(rows, key=lambda r: r[:3])


def split_payment_tax(money_in, account, payment_tax):
    """(merchandise_tax, deferred): the part of an order payment's tax counted on its money-in part today and the
    part deferred with its Account part (a charge on the customer's ledger)."""
    merchandise_tax = payment_tax if not account else share(money_in, payment_tax, money_in + account)
    return merchandise_tax, payment_tax - merchandise_tax


def order_payment_figures(order, money_in, account, payment_tax, shares):
    """Split an order payment's tax and taxable base between its money-in part and its Account part, the way the
    ledger always has: (merchandise_tax, deferred, taxable_in, account_taxable). The money-in part's base keeps the
    whole pre-tax amount when the order has no taxed lines in `shares` (the Sales Tax rule); the Account part's
    keeps zero (the charge's rule)."""
    merchandise_tax, deferred = split_payment_tax(money_in, account, payment_tax)
    pretax = money(order.total or ZERO) - money(order.tax or ZERO)
    taxable_in = ZERO
    if order.tax and pretax:
        taxable_in = money((money_in - merchandise_tax) * min(shares.get(order.pk, pretax), pretax) / pretax)
    account_taxable = order_taxable(order, account, deferred, shares) if account else ZERO
    return merchandise_tax, deferred, taxable_in, account_taxable


def tax_lock_for(order, money_in, account, payment_tax, shares, tax_location):
    """The TaxLock to store on a payment of `money_in` + `account` that books `payment_tax` on `order` as it stands."""
    merchandise_tax, deferred, taxable_in, account_taxable = order_payment_figures(order, money_in, account, payment_tax, shares)
    amount = money_in + account
    taxable = taxable_in + account_taxable
    return TaxLock(payment_tax, taxable, amount - payment_tax - taxable, account_taxable, section_of(tax_location, order.tax_exempt))


def tax_section_for_new(lock, earlier):
    """A payment recorded from now on lands in the order's section, except that one carrying tax on an order that is
    Exempt NOW (a reversal after a Tax-exempt toggle, or tax still due from before it) is reported where that tax
    was booked — the section of the order's `earlier` locks, the default location failing that — so the exempt section never
    shows tax. Backfilled locks keep the section the reports always used."""
    if lock.tax and is_exempt_location(lock.location):
        return lock._replace(location=next((e.location for e in earlier if e.tax), taxed_location()))
    return lock


def payment_tax_location(row, order):
    """The report section an order payment lands in: the deposit receipt's own QuickBooks tax location when it has
    one, else the order's (`_parse`)."""
    if isinstance(row, Document) and row.tax_location:
        return row.tax_location
    return location(order)


def order_tax_locks(order, shares=None, new=None, rows=None, cancelled=None):
    """What the ledger books on each counted payment of `order`: {(kind, pk): TaxLock}. Locked payments keep their
    stored figures; the others get the behavior before payment tax locking run in order — the cumulative share of the order's CURRENT tax
    (`cumulative_order_tax`), the same arithmetic `replay` uses, so a backfill of these values changes no report.
    `new` = the (kind, pk) of a payment just recorded: it is left out of that run and then booked as the difference
    between the order's tax on everything paid (as the order stands today) and what the other payments already
    carry, so the order's payments always add up to its tax to the cent — the payment that completes the order
    carries the remainder of a later edit. A refund (`new` with a negative amount) follows `refund_tax_lock` (F2); `cancelled` = the order is being cancelled (its final figures are its pickups').
    """
    rows = counted_order_payments(order) if rows is None else rows
    shares = shares if shares is not None else {}
    if order.pk not in shares:
        shares.update(order_taxable_shares([order.pk]))
        shares.setdefault(order.pk, ZERO)
    order_total, order_tax = money(order.total or ZERO), money(order.tax or ZERO)
    out, paid, taxed = {}, ZERO, ZERO
    for when, kind, pk, row, money_in, account in rows:
        if (kind, pk) == new:
            continue
        paid += money_in + account
        lock = row.tax_lock
        if lock is None:
            target = cumulative_order_tax(paid, order_tax, order_total)
            lock = tax_lock_for(order, money_in, account, target - taxed, shares, payment_tax_location(row, order))
        taxed += lock.tax
        out[(kind, pk)] = lock
    if new is not None:
        for when, kind, pk, row, money_in, account in rows:
            if (kind, pk) == new:
                earlier = list(out.values())
                if money_in + account < 0:
                    lock = refund_tax_lock(order, money_in, account, paid, earlier, shares, cancelled)
                else:
                    paid += money_in + account
                    target = cumulative_order_tax(paid, order_tax, order_total)
                    lock = tax_lock_for(order, money_in, account, target - taxed, shares, payment_tax_location(row, order))
                out[(kind, pk)] = tax_section_for_new(lock, earlier)
    return out


def order_final_figures(order, shares=None, cancelled=None):
    """(total, tax, taxable) the tax locks on an order's payments must add up to over the order's life (booked tax = the order's final tax): the order as it stands, or — once it is cancelled — the completed app
    pickup receipts' figures (what was delivered is what was sold; nothing when nothing was picked up)."""
    if cancelled is None:
        cancelled = order.status == 'cancelled'
    if cancelled:
        total = tax = taxable = ZERO
        for receipt in order.fulfillments.filter(native_source(), status='completed').prefetch_related('lines'):
            total += money(receipt.total or ZERO)
            tax += money(receipt.tax or ZERO)
            if not receipt.tax_exempt:
                taxable += sum((line.net_amount for line in receipt.lines.all()
                                if line.item_name != POA_NAME and line.tax_rate and line.tax_rate > 0), ZERO)
        return total, tax, taxable
    shares = shares if shares is not None else {}
    if order.pk not in shares:
        shares.update(order_taxable_shares([order.pk]))
        shares.setdefault(order.pk, ZERO)
    total, tax = money(order.total or ZERO), money(order.tax or ZERO)
    pretax = total - tax
    taxable = min(shares[order.pk], pretax) if tax and pretax else ZERO
    return total, tax, taxable


def clamp_reversal(value, booked):
    """A refund's tax (or taxable base) never has the opposite sign of a reversal and never reverses more than is
    booked: between -booked and 0 (0 when nothing positive is booked)."""
    return min(max(value, min(-booked, ZERO)), ZERO)


def refund_tax_lock(order, money_in, account, paid_before, earlier, shares, cancelled=None):
    """The TaxLock for a refund of `money_in` + `account` (negative) on `order`, whose
    other payments (`earlier` locks, `paid_before` money) are already booked.
    - It leaves 0 < money still paid < the order's final total: reverse tax at the ratio actually booked on the
      order's payments, computed cumulatively (what stays booked = booked × paid after ÷ paid before, half-up),
      clamped so the refund never books positive tax and never reverses more than is booked. Same for the taxable
      base.
    - It empties the order, or leaves it paid in full or overpaid: settle the order's booked tax and taxable base to
      the exact final figures (`order_final_figures`; a cancelled order's are its pickups').
    The refund's Account part carries its taxable base as the charge's (deferred) base, as any Account payment."""
    amount = money_in + account
    paid_after = paid_before + amount
    final_total, final_tax, final_taxable = order_final_figures(order, shares, cancelled)
    booked_tax = sum((e.tax for e in earlier), ZERO)
    booked_taxable = sum((e.taxable for e in earlier), ZERO)
    if ZERO < paid_after < final_total and paid_before > ZERO:
        def keep(value):
            return money(value * paid_after / paid_before)
        tax = keep(booked_tax) - booked_tax
        taxable = keep(booked_taxable) - booked_taxable
    else:
        full = paid_after >= final_total
        tax = (final_tax if full else ZERO) - booked_tax
        taxable = (final_taxable if full else ZERO) - booked_taxable
    # A refund row never carries positive tax (money went out, none came in): what a settlement still owes above
    # what is booked is the settlement adjustment's job (`settle_order_tax`, which runs right after).
    tax = clamp_reversal(tax, booked_tax)
    taxable = clamp_reversal(taxable, booked_taxable)
    return TaxLock(tax, taxable, amount - tax - taxable, taxable if account else ZERO,
                   section_of(location(order), order.tax_exempt))


def lock_payment(order, payment, cancelled=None):
    """Store the tax figures on `payment` (an OrderPayment just recorded, re-recorded or refunded on `order`) for the
    order as it stands now:, the tax follows the money on the day it moves. Called inside the saving transaction.
    `cancelled` = True for the deposit refund of an order being cancelled (F2: settles to its pickups' tax)."""
    payment.set_tax_lock(order_tax_locks(order, new=(1, payment.pk), cancelled=cancelled)[(1, payment.pk)])
    payment.save(update_fields=list(payment.LOCK_FIELDS))


ADJUSTMENT_REFERENCE = 'Sales tax adjustment'


def settle_order_tax(order, after_refund=False):
    """Once an order is completed, cancelled, or an edit leaves its balance at exactly $0,
    the tax booked on its payments over its life must equal its final tax (`order_final_figures`). Any difference is
    booked TODAY as a $0 OrderPayment carrying only a tax lock (`adjustment`, "Sales tax adjustment"): tax ± the
    difference, the taxable base likewise, the non-taxable base the balancing figure (the parts sum to 0), in the
    section the order's tax was booked in. The part that was deferred with an Account-paid deposit is adjusted on the
    Account side (a second row, method 'account', applied to the customer's open charge by `Ledger.adjust_charge`),
    not as collected tax. An overpaid order waits for its refund: F2 settles the refund row to the final figures, and
    `after_refund` (part 5, re-review finding 2: the refund just recorded still leaves the order paid in full or
    overpaid) books here whatever the refund row could not carry — a refund never carries positive tax. Ordinary
    saves and payments on an overpaid order create nothing. Orders the review guards still hold (a QuickBooks
    deposit mismatch, a deposit QuickBooks already used on a pickup there) are left alone. Called from
    services.recalculate_order and the order cancel path, inside their transaction. Returns the rows created."""
    if order.kind != 'order' or order.status == 'held':
        return []
    cancelled = order.status == 'cancelled'
    if not (cancelled or order.status == 'completed' or order.balance_due == ZERO or (after_refund and order.balance_due < ZERO)):
        return []
    if order.qb_deposit_mismatch or (order.deposit_used or ZERO) > 0:
        return []
    rows = counted_order_payments(order)
    if not rows:
        return []
    shares = {}
    locks = order_tax_locks(order, shares, rows=rows)
    final_total, final_tax, final_taxable = order_final_figures(order, shares, cancelled)
    booked_tax = sum((lock.tax for lock in locks.values()), ZERO)
    if booked_tax == final_tax:
        return []
    booked_taxable = sum((lock.taxable for lock in locks.values()), ZERO)
    booked_account_taxable = sum((lock.account_taxable for lock in locks.values()), ZERO)
    money_in_total = sum((money_in for _, _, _, _, money_in, _ in rows), ZERO)
    account_total = sum((account for _, _, _, _, _, account in rows), ZERO)
    booked_deferred = ZERO
    for when, kind, pk, row, money_in, account in rows:
        lock = locks[(kind, pk)]
        if kind == 1 and row.adjustment:
            booked_deferred += lock.tax if tender_key(row.method) == 'account' else ZERO
        else:
            booked_deferred += split_payment_tax(money_in, account, lock.tax)[1]
    paid = money_in_total + account_total
    if account_total and paid > ZERO:
        deferred_target = final_tax if money_in_total <= ZERO else share(account_total, final_tax, paid)
        deferred_taxable_target = final_taxable if money_in_total <= ZERO else share(account_total, final_taxable, paid)
    else:
        deferred_target = deferred_taxable_target = ZERO
    deferred_adj = deferred_target - booked_deferred
    deferred_taxable_adj = deferred_taxable_target - booked_account_taxable
    collected_adj = (final_tax - booked_tax) - deferred_adj
    collected_taxable_adj = (final_taxable - booked_taxable) - deferred_taxable_adj
    earlier = list(locks.values())
    now = timezone.now()
    created = []
    for method, tax, taxable in (('other', collected_adj, collected_taxable_adj), ('account', deferred_adj, deferred_taxable_adj)):
        if not tax:
            continue
        lock = TaxLock(tax, taxable, -tax - taxable, taxable if method == 'account' else ZERO, section_of(location(order), order.tax_exempt))
        payment = OrderPayment(order=order, amount=ZERO, method=method, reference=ADJUSTMENT_REFERENCE, adjustment=True, created_at=now)
        # The Account row reports no tax itself: its lock keeps the order's own section (Exempt included) so the replay
        # can move the still-open charge there (`Ledger.adjust_charge`); the true-up it counts today is always under the default location.
        payment.set_tax_lock(lock if method == 'account' else tax_section_for_new(lock, earlier))
        payment.save()
        created.append(payment)
    return created


def lock_order(order, shares=None):
    """Backfill: store the ledger's current figures on every counted payment of `order` that has none (never
    overwriting a stored value). Returns (rows locked, rows already locked)."""
    rows = counted_order_payments(order)
    locks = order_tax_locks(order, shares, rows=rows)
    locked = already = 0
    for when, kind, pk, row, money_in, account in rows:
        if row.tax_lock is not None:
            already += 1
            continue
        row.set_tax_lock(locks[(kind, pk)])
        row.save(update_fields=list(row.LOCK_FIELDS))
        locked += 1
    return locked, already


def lock_all_orders(progress=None):
    """Backfill every order with a counted payment (the importer's post-load step and `lock_order_payment_tax`).
    Returns counts: orders, payments locked, payments already locked."""
    order_ids = set(Document.objects.filter(kind='sale', status='completed', receipt_type__in=(2, 3), deposit_order__isnull=False)
                    .values_list('deposit_order_id', flat=True))
    order_ids |= set(OrderPayment.objects.filter(order__kind='order').values_list('order_id', flat=True))
    counts = dict(orders=0, payments_locked=0, payments_already_locked=0)
    shares = {}
    for n, order in enumerate(Document.objects.filter(pk__in=order_ids).order_by('pk').iterator(chunk_size=500), start=1):
        locked, already = lock_order(order, shares)
        counts['orders'] += 1
        counts['payments_locked'] += locked
        counts['payments_already_locked'] += already
        if progress and n % 1000 == 0:
            progress(f'{n} orders')
    return counts


def estimate_context(doc):
    """Replay only the estimate terms stored with the document, never today's location rate."""
    return doc.account_estimate_rate, section_of(doc.account_estimate_location, False)


def estimate(amount, exempt, rate=None):
    """Tax inside money received with no charge history to match: exactly r/(100 + r) for the default location's rate r
    (7/107 at 7 %), never a rounded factor like the sheet's 0.06542."""
    if exempt:
        return ZERO
    rate = shop_tax().default_location.rate if rate is None else rate
    return money(amount * rate / (100 + rate))


def tender_key(method):
    return {'cash': 'cash', 'check': 'check', 'credit': 'card', 'debit': 'card',
            'account': 'account'}.get(method, 'gift')


def tenders(doc):
    """Signed tender amounts by kind for a receipt; `missing` when an amount is unknown (never invented).
    One undecodable tender amount beside a known receipt total is solved as total − the other tenders (QB's tenders
    always add up to the total); two or more unknowns, or an unknown total, stay unknown."""
    out = dict.fromkeys(TENDERS, ZERO)
    missing = False
    if doc.from_quickbooks:
        payments = [(t.method, t.amount) for t in doc.tenders.all() if not t.use_in_total]
    else:
        payments = [(p['method'], Decimal(str(p['amount']))) for p in doc.payment_breakdown]
    unknown = [i for i, (_, amount) in enumerate(payments) if amount is None]
    if len(unknown) == 1 and doc.total is not None:
        known = sum((money(a) for _, a in payments if a is not None), ZERO)
        payments[unknown[0]] = (payments[unknown[0]][0], money(doc.total) - known)
        doc._derived_tender = True
    for method, amount in payments:
        if amount is None:
            missing = True
        else:
            out[tender_key(method)] += money(amount)
    if not doc.from_quickbooks and payments and doc.tendered is not None and doc.total is not None:
        # App breakdown stores cash handed over, not net cash retained.
        out['cash'] -= max(money(doc.tendered - doc.total), ZERO)
    return out, missing or (not payments and bool(doc.total))


@dataclass(eq=False)
class Lot:
    """Money on a customer's ledger with what it is made of: tax + taxable + non-taxable = amount, the tax location of
    the charge it came from, and the charge key ('sale'/'order_payment', pk). Four roles share the shape: an open
    charge (`Ledger.lots`), an unapplied credit (`Ledger.credits`), an allocation — the part of a payment that settled
    one charge (`Ledger.paid`, and `Event.allocations`) — and a signed adjustment (`Event.adjustments`: a credit's
    estimate reversed and the charge it really funded, see `Ledger._settle`). `key` None = money QuickBooks knows but
    the data has no receipt for (an opening balance, see `unknown_debt`); False = a prepayment estimated at 7/107 or
    zero until a charge arrives to say what it bought. `kind` names a credit: 'prepaid' (a payment beyond every known
    charge), 'return' (a return credited to the account, carrying the returned merchandise's own tax, bases and
    location) or 'opening' (credit QuickBooks held at the backup). `qb` marks money QuickBooks' balance at the backup
    accounts for (a charge on an imported receipt, debt from before the data), the only lots a QuickBooks adjustment
    with no receipt in the data can have settled (see `Ledger.reconcile`)."""
    amount: Decimal
    tax: Decimal
    taxable: Decimal
    location: str
    key: object = None
    kind: str = ''
    qb: bool = False

    @property
    def nontaxable(self):
        return self.amount - self.tax - self.taxable

    @property
    def estimated(self):
        return self.key is None or self.key is False

    def part(self, take):
        """The first `take` of this lot: proportional tax and taxable base, half-up; the last cent goes with the
        closing part, so a lot's parts always add up to the lot."""
        if take == self.amount:
            return Lot(take, self.tax, self.taxable, self.location, self.key, self.kind, self.qb)
        return Lot(take, share(take, self.tax, self.amount), share(take, self.taxable, self.amount), self.location, self.key, self.kind, self.qb)

    def shrink(self, part):
        self.amount -= part.amount
        self.tax -= part.tax
        self.taxable -= part.taxable

    def negated(self):
        return Lot(-self.amount, -self.tax, -self.taxable, self.location, self.key, self.kind, self.qb)


def estimated_lot(amount, exempt, key=False, kind='', qb=False, context=None):
    """Money with no charge behind it in the data: the estimate's tax on a taxable customer's (all taxable), none on
    an exempt one (all non-taxable)."""
    rate, place = context if context is not None else (None, taxed_location())
    tax_part = estimate(amount, exempt, rate)
    return Lot(amount, tax_part, ZERO if exempt else amount - tax_part, exempt_location() if exempt else place, key, kind, qb)


def fit(part, lot, take):
    """The first `take` of an open charge `lot` as the returned merchandise `part` describes it: the return's own tax
    and taxable base, as far as the charge still carries them, and never more than the rest of the charge can give up
    (what stays owed must still hold its own tax and bases). Both sides keep tax + taxable + non-taxable = amount."""
    rest = lot.amount - take
    tax = min(max(part.tax, lot.tax - rest), lot.tax)
    taxable = min(max(part.taxable, take - tax - lot.nontaxable), lot.taxable, take - tax)
    return Lot(take, tax, taxable, lot.location, lot.key, lot.kind, lot.qb)


class Ledger:
    """One customer's open charges, oldest first: transient FIFO lots, never a balance write.

    Open lots and unapplied credits never coexist: a credit (return credited to the account, a payment beyond every
    known charge, or credit QuickBooks held at the backup) is applied to the oldest open charges at once, and a new
    charge is funded from any credit before it opens a lot. Open lots − credits = the customer's account balance at
    every step; nothing is ever left behind in a side pocket (a tax residual is reported the day it arises, under the
    location it belongs to — review finding 2)."""

    def __init__(self):
        self.lots = deque()          # open charges, oldest first
        self.credits = deque()       # unapplied credits, oldest first
        self.paid = []               # allocations in order (payments and funded charges), for refunds to undo newest first
        self.adjustments = []        # this event's true-ups: signed Lots, each credit used (reversed) and charge funded
        self.cancelled = []          # this event's returns of merchandise still owed: the parts taken off their charges
        self.settled = []            # what QuickBooks settled at the backup with no payment in the data (`reconcile`)
        self.charged = defaultdict(lambda: ZERO)   # charge key -> money charged over its life (net of a keyed refund), for `adjust_charge`

    @property
    def reconciled(self):
        """Tax trued up by this event: the funded charges' own tax less the estimate the credits used carried."""
        return sum((a.tax for a in self.adjustments), ZERO)

    def charge(self, lot, returned=()):
        """Open a charge (or, with a negative amount, credit the account — a return credited instead of refunded).
        `returned` describes a return's merchandise by the charge it was sold on (Lots keyed like the charges): what
        is still owed on that charge comes off it first — its tax was never collected, so nothing is counted or
        reversed (review 3, finding 1) — and only the rest is a credit, carrying the rest of the return's tax (the
        part a cash payment or an earlier payment on account did collect) until it funds a charge or is paid back.
        Returns this event's adjustments: a credit was taxed (or, for a return, reversed) at what it was when the money
        moved; the charge it funds says what that money really bought, and the difference counts on the charge's day;
        a return's collected tax that no credit remains to carry is reversed on the return's own day."""
        self.adjustments, self.cancelled = [], []
        if not lot.estimated:
            self.charged[lot.key] += lot.amount
        if lot.amount < 0:
            credit = Lot(-lot.amount, -lot.tax, -lot.taxable, lot.location, lot.key, 'return')
            for group in returned:
                for open_ in [o for o in self.lots if o.key == group.key]:
                    take = min(group.amount, open_.amount, credit.amount)
                    if take <= 0:
                        break
                    gone = fit(group.part(take), open_, take)
                    open_.shrink(gone)
                    group.shrink(group.part(take))
                    credit.shrink(gone)
                    self.cancelled.append(gone)
                    if not open_.amount:
                        self.lots.remove(open_)
            tax = min(max(credit.tax, ZERO), credit.amount)
            taxable = min(max(credit.taxable, ZERO), credit.amount - tax)
            if (credit.tax, credit.taxable) != (tax, taxable):
                self.adjustments.append(Lot(ZERO, tax - credit.tax, taxable - credit.taxable, credit.location, credit.key, 'cancelled'))
                credit.tax, credit.taxable = tax, taxable
            if credit.amount:
                self.credits.append(credit)
        elif lot.amount:
            self.lots.append(lot)
        self._settle()
        return self.adjustments

    def reconcile(self, amount, exempt, context=None):
        """QuickBooks' balance at the backup is `amount` below what the imported receipts explain: an adjustment
        QuickBooks made with no receipt in the data. Applied at the backup moment, it first settles what imported
        charges still show as owed, oldest first (no money received, no tax — flagged in `settled`); only what is left
        beyond them is credit QuickBooks holds for the customer, funding later charges silently (money taxed before the
        data, estimated only if it is ever paid back). Review 3, finding 2: an unexplained difference is never
        spendable history that changes which charge a real payment inside the data paid."""
        remaining = amount
        for lot in [o for o in self.lots if o.qb]:
            if remaining <= 0:
                break
            part = lot.part(min(remaining, lot.amount))
            lot.shrink(part)
            remaining -= part.amount
            self.settled.append(part)
            if not lot.amount:
                self.lots.remove(lot)
        if remaining > 0:
            self.credits.append(estimated_lot(remaining, exempt, key=None, kind='opening', context=context))
            self.adjustments = []
            self._settle()

    def _settle(self):
        """Fund the oldest open charges from the oldest credits. Each funded part becomes an allocation (so a refund
        can reopen the charge) and, unless the credit is QuickBooks' opening credit (its money moved before the data
        and was taxed then), two signed adjustments: the credit's own tax / bases / location reversed and the charge's
        own in their place — zero money overall, the right tax under the right location (review findings 2 and 4)."""
        while self.credits and self.lots:
            credit, lot = self.credits[0], self.lots[0]
            take = min(credit.amount, lot.amount)
            funded, used = lot.part(take), credit.part(take)
            lot.shrink(funded)
            credit.shrink(used)
            if credit.kind != 'opening':
                self.adjustments.append(Lot(-used.amount, -used.tax, -used.taxable, used.location, used.key, credit.kind))
                self.adjustments.append(Lot(funded.amount, funded.tax, funded.taxable, funded.location, funded.key, credit.kind))
            self.paid.append(funded)
            if not lot.amount:
                self.lots.popleft()
            if not credit.amount:
                self.credits.popleft()

    def pay(self, amount, exempt, context=None):
        """Apply a payment on account; returns its allocations: one Lot per charge it settles (oldest first), the
        estimated remainder last. A refund (negative) first hands back unapplied credit (newest first), then undoes the
        newest allocations and reopens their charges; what is left paid back a payment older than the data, so that
        debt reopens as the oldest lot at the same estimate (review 3, finding 3: balance, ledger and agree)."""
        out = []
        self.adjustments = []
        if amount < 0:
            remaining = -amount
            while remaining and self.credits:
                credit = self.credits[-1]
                part = credit.part(min(remaining, credit.amount))
                credit.shrink(part)
                remaining -= part.amount
                if not credit.amount:
                    self.credits.pop()
                out.append(part.negated())
            while remaining and self.paid:
                last = self.paid[-1]
                part = last.part(min(remaining, last.amount))
                last.shrink(part)
                remaining -= part.amount
                if not last.amount:
                    self.paid.pop()
                self.lots.appendleft(Lot(part.amount, part.tax, part.taxable, part.location, part.key, qb=part.qb))
                out.append(part.negated())
            if remaining:
                reopened = estimated_lot(remaining, exempt, key=None, qb=True, context=context)
                self.lots.appendleft(reopened)
                out.append(reopened.negated())
            return out
        remaining = amount
        while remaining and self.lots:
            lot = self.lots[0]
            part = lot.part(min(remaining, lot.amount))
            lot.shrink(part)
            remaining -= part.amount
            if not lot.amount:
                self.lots.popleft()
            self.paid.append(part)
            out.append(Lot(part.amount, part.tax, part.taxable, part.location, part.key))
        if remaining:
            # Beyond every known charge: held as credit for the customer's next charge, taxed now at the estimate.
            self.credits.append(estimated_lot(remaining, exempt, kind='prepaid', context=context))
            out.append(estimated_lot(remaining, exempt, context=context))
        return out

    def balance(self):
        return sum((lot.amount for lot in self.lots), ZERO) - sum((c.amount for c in self.credits), ZERO)

    def adjust_charge(self, keys, tax, taxable, section):
        """Apply a no-money correction to the charge's original jurisdictions.

        Unpaid shares stay deferred. Paid shares are corrected where they were collected and updated in place so
        a later refund reverses the corrected figures. Tax reversals follow booked tax; base reversals follow booked
        bases. Positive changes follow money when there is no corresponding booked component.
        """
        self.adjustments = []
        open_ = [lot for lot in self.lots if lot.key in keys]
        paid = [lot for lot in self.paid if lot.key in keys]
        lots = open_ + paid
        if not lots:
            if tax or taxable:
                self.adjustments.append(Lot(ZERO, tax, taxable, section, None, 'adjustment'))
            return self.adjustments

        def portions(change, attribute):
            weights = [max(getattr(lot, attribute), ZERO) for lot in lots]
            if change >= 0 or not sum(weights, ZERO):
                weights = [lot.amount for lot in lots]
            total = sum(weights, ZERO)
            left = change
            result = []
            for n, weight in enumerate(weights):
                part = left if n == len(weights) - 1 else share(weight, change, total)
                result.append(part)
                left -= part
            return result

        for lot, tax_part, base_part in zip(lots, portions(tax, 'tax'), portions(taxable, 'taxable')):
            original_location = lot.location
            if is_exempt_location(original_location) and tax_part:
                # A newly taxable charge has no earlier taxable jurisdiction; use its new one.
                original_location = section
            new_tax = min(max(lot.tax + tax_part, ZERO), lot.amount)
            new_base = min(max(lot.taxable + base_part, ZERO), lot.amount - new_tax)
            if lot in open_:
                # Anything an unpaid lot cannot hold is a correction in its original jurisdiction.
                correction_tax = tax_part - (new_tax - lot.tax)
                correction_base = base_part - (new_base - lot.taxable)
                lot.location = original_location if is_exempt_location(section) and new_tax else section
            else:
                correction_tax, correction_base = tax_part, base_part
                lot.location = original_location
            lot.tax, lot.taxable = new_tax, new_base
            if correction_tax or correction_base:
                self.adjustments.append(Lot(ZERO, correction_tax, correction_base, original_location, lot.key, 'adjustment'))
        return self.adjustments

    def open_by_key(self):
        """Still-owed amount per charge key, oldest first (keys None are merged under None)."""
        out = {}
        for lot in self.lots:
            out[lot.key] = out.get(lot.key, ZERO) + lot.amount
        return out


@dataclass
class Event:
    when: datetime
    kind: str                       # 'receipt' | 'poa' | 'deposit' | 'order_payment'
    doc: object                     # the receipt, or the order for a native order payment
    order: object = None            # the customer order for deposits / order payments
    source: object = None           # the OrderPayment for kind 'order_payment'
    tender: dict = field(default_factory=dict)
    received: Decimal = ZERO        # cash + check + card, net of change
    gift: Decimal = ZERO            # certificate redemptions: not money received, see module docstring
    money_in: Decimal = ZERO        # received + gift: the part of the document taxed today
    account: Decimal = ZERO         # charged to the customer's account (tax deferred)
    tax: Decimal = ZERO             # tax counted on this day (merchandise share + any payment-on-account share)
    merchandise_tax: Decimal = ZERO
    payment: Decimal = ZERO         # payment on account applied to the ledger
    payment_tax: Decimal = ZERO
    credit: Decimal = ZERO          # return value credited to the account instead of refunded
    deferred: Decimal = ZERO        # tax charged to account on this document
    estimated: Decimal = ZERO       # part of `payment` matched by no known charge (tax estimated at 7/107 or 0)
    estimated_tax: Decimal = ZERO
    charged: Decimal = ZERO         # what this document puts on the ledger (Account tender less any return credit)
    allocations: list = field(default_factory=list)   # the payment split by the charges it settled (Lots, oldest first)
    adjustments: list = field(default_factory=list)   # true-ups today: credits used (reversed) and the charges they funded (signed Lots)
    cancelled: list = field(default_factory=list)     # a return's merchandise that was still owed: taken off its charge, no tax (Lots)
    reconcile: Decimal = ZERO       # after this (the customer's last imported) event: QuickBooks' backup balance below the receipts, see `Ledger.reconcile`
    exempt_received: Decimal = ZERO  # Received under the Exempt location (by the paid charges for a payment on account)
    lot_location: str = field(default_factory=taxed_location)  # report section of this document's charge (a location name)
    reconciled: Decimal = ZERO      # tax trued up today: Σ tax of `adjustments` (a credit's estimate met the charge it funded)
    customer_exempt: bool = False
    tax_location: str = field(default_factory=taxed_location)
    new_sale: Decimal = ZERO
    poa_amount: Decimal = ZERO
    is_deposit: bool = False
    poa: bool = False               # receipt is only a payment on account
    mixed: bool = False             # merchandise and a payment on account on one receipt
    capped: bool = False            # order payment beyond the order's total (no tax on the excess)
    lock: object = None             # TaxLock stored on the deposit receipt / order payment (figures fixed when received)
    adjustment: bool = False        # a $0 order-payment row carrying only a tax lock (settle_order_tax)
    taxable: Decimal = ZERO         # order payment: taxable base inside its money-in part (Sales Tax row)
    account_taxable: Decimal = ZERO  # order payment: taxable base inside its Account part (the charge's lot)
    lines: list = field(default_factory=list)
    excluded: str = ''              # 'payout' | 'pickup' | 'applied' | 'unlinked' | 'missing' (unlinked/missing still listed)
    in_period: bool = True

    @property
    def day(self):
        return timezone.localdate(self.when)

    @property
    def target(self):
        return self.order or self.doc


@dataclass
class Walk:
    events: list
    excluded: dict
    estimated_count: int = 0
    exempt_estimated_count: int = 0
    estimated_total: Decimal = ZERO     # payments on account with no known charge
    estimated_tax: Decimal = ZERO
    deferred_total: Decimal = ZERO      # charged to account this period (money)
    deferred_tax: Decimal = ZERO
    poa_total: Decimal = ZERO           # payments on account received this period
    poa_tax: Decimal = ZERO             # tax they released, plus any prepayment estimate trued up this period
    reconciled_tax: Decimal = ZERO      # the true-up part: prepayments met the charges they funded
    gift_total: Decimal = ZERO
    gift_tax: Decimal = ZERO
    applied_total: Decimal = ZERO       # legacy deposits applied on a pickup receipt (excluded)
    cancelled_total: Decimal = ZERO     # returned merchandise that was still owed, taken off its charge this period (no tax collected, none counted)
    cancelled_tax: Decimal = ZERO
    ledgers: dict = field(default_factory=dict)


def _doc_events(docs):
    return [(d.completed_at, 0, d.pk, d) for d in docs]


def _payment_events(payments):
    return [(p.created_at, 1, p.pk, p) for p in payments]


def _receipt_query():
    lines = Line.objects.only(*LINE_FIELDS).annotate(origin_document_id=F('returned_from_line__document_id'))
    return (Document.objects.filter(kind='sale', status='completed')
            .select_related('customer', 'deposit_order')
            .prefetch_related('tenders', Prefetch('lines', queryset=lines)))


def _order_payment_query():
    # Order payments made in this app on app-made AND imported (QuickBooks) orders. A QB order's own deposits
    # are deposit receipts (`deposit_order`), replayed by the same cumulative per-order share, so an app payment on
    # top of them counts only its own part of the order's tax and the QB deposit is never counted twice.
    return (OrderPayment.objects.filter(order__kind='order')
            .select_related('order', 'order__customer'))


def period_events(lower, upper):
    """Every event up to `upper`, with full history for the customers whose account the period touches (a payment
    on account or a charge to it — a charge may be funded by an earlier prepayment or return credit) and for the
    orders receiving a payment in the period (so FIFO and the per-order remainder are exact)."""
    period_receipts = Document.objects.filter(kind='sale', status='completed', completed_at__gte=lower, completed_at__lt=upper)
    customers = set(Line.objects.filter(document__in=period_receipts, item_name=POA_NAME)
                    .exclude(document__customer_id=None).values_list('document__customer_id', flat=True))
    customers |= set(Tender.objects.filter(document__in=period_receipts, method='account', use_in_total=False)
                     .exclude(document__customer_id=None).values_list('document__customer_id', flat=True))
    customers |= {doc.customer_id for doc in period_receipts.filter(native_source(), customer_id__isnull=False)
                  .only('customer_id', 'payment_breakdown')
                  if any(p.get('method') == 'account' for p in doc.payment_breakdown or [])}
    customers |= set(OrderPayment.objects.filter(created_at__gte=lower, created_at__lt=upper, method='account', order__kind='order')
                     .exclude(order__customer_id=None).values_list('order__customer_id', flat=True))
    orders = set(period_receipts.filter(receipt_type__in=(2, 3)).exclude(deposit_order_id=None)
                 .values_list('deposit_order_id', flat=True))
    orders |= set(OrderPayment.objects.filter(created_at__gte=lower, created_at__lt=upper, order__kind='order')
                 .values_list('order_id', flat=True))
    docs = (_receipt_query()
            .filter(Q(completed_at__gte=lower, completed_at__lt=upper) | Q(customer_id__in=customers, completed_at__lt=upper)
                    | (Q(customer_id__in=customers) & ~native_source()) | Q(deposit_order_id__in=orders, completed_at__lt=upper)))
    payments = (_order_payment_query().filter(created_at__lt=upper)
                .filter(Q(created_at__gte=lower) | Q(order__customer_id__in=customers) | Q(order_id__in=orders)))
    return _doc_events(docs) + _payment_events(payments)


def customer_events(customer_id):
    docs = _receipt_query().filter(customer_id=customer_id)
    payments = _order_payment_query().filter(order__customer_id=customer_id)
    return _doc_events(docs) + _payment_events(payments)


def section_of(location, exempt):
    """The report section a document's tax location lands in: its own if recognized, else by its exempt flag."""
    return shop_tax().section_of(location, exempt)


def taxable_net(ev):
    """A receipt's taxable amount: item lines whose tax rate > 0 (qty × exact unit price, half-up) less each line's share
    of the receipt discount (QB takes it off before computing tax); the whole receipt discount comes off instead on an
    imported receipt whose lines carry no per-line shares yet (before `backfill_unit_prices`). Negative on a return."""
    taxable = sum((line.net_amount for line in ev.lines if line.item_name != POA_NAME and line.tax_rate and line.tax_rate > 0), ZERO)
    discount = money(ev.doc.discount or ZERO)
    if taxable and discount and not any(line.receipt_discount for line in ev.lines):
        taxable -= discount
    return taxable


def receipt_figures(ev):
    """(gross, discount, taxable, nontaxable) for the merchandise money-in share of a sales receipt. Subtotal is gross
    on every receipt (Total = Subtotal − Discount + Tax); a receipt paid in full shows exactly QB's figures,
    a part-charged one the pre-tax share received."""
    doc = ev.doc
    gross = money(doc.subtotal or ZERO) - ev.poa_amount
    discount = money(doc.discount or ZERO)
    taxable = taxable_net(ev)
    basis = ev.money_in - ev.payment             # merchandise money in (a mixed receipt's payment part is its own row)
    full = ev.new_sale
    if basis == full:
        return gross, discount, taxable, gross - discount - taxable
    pretax_full = full - money(doc.tax or ZERO)
    pretax = basis - ev.merchandise_tax
    if not pretax_full:
        return ZERO, ZERO, ZERO, ZERO
    gross_r = money(gross * pretax / pretax_full)
    taxable_r = money(taxable * pretax / pretax_full)
    return gross_r, gross_r - pretax, taxable_r, pretax - taxable_r


def charged_taxable(ev, amount, tax):
    """Taxable base inside the part of a receipt charged to account (`amount` incl. its deferred `tax`): the receipt's
    taxable amount less what the money-in share reports, so the two parts add up to the receipt exactly."""
    doc = ev.doc
    full = ev.new_sale
    pretax_full = full - money(doc.tax or ZERO)
    if not pretax_full:
        return ZERO
    taxable = taxable_net(ev)
    if (ev.money_in - ev.payment) + amount == full:
        return taxable - receipt_figures(ev)[2]
    return money(taxable * (amount - tax) / pretax_full)


def returned_groups(ev):
    """The merchandise a return credited to the account brings back, by the receipt it was sold on — QuickBooks' own
    link on imported return lines (`returned_from_receipt`, DocRefSID), the returned line's receipt on app returns:
    one Lot per original charge key with the group's value (its lines net of discount plus their share of the
    return's tax, split by taxed value, the last group taking the remainder) and taxable base. Lines with no link to
    an original receipt stay in the credit: their history is unknown, so the credit keeps carrying their tax."""
    groups = {}
    returned = [line for line in ev.lines if line.item_name != POA_NAME and line.net_amount < 0]
    for line in returned:
        origin = line.returned_from_receipt_id or line.origin_document_id
        if origin:
            group = groups.setdefault(('sale', origin), [ZERO, ZERO])
            group[0] -= line.net_amount
            if line.tax_rate and line.tax_rate > 0:
                group[1] -= line.net_amount
    if not groups:
        return []
    tax = max(-ev.deferred, ZERO)
    all_taxable = sum((-line.net_amount for line in returned if line.tax_rate and line.tax_rate > 0), ZERO)
    linked_taxable = sum((taxable for _, taxable in groups.values()), ZERO)
    out, left = [], tax
    for n, (key, (pre, taxable)) in enumerate(groups.items(), start=1):
        last = n == len(groups) and linked_taxable == all_taxable
        part = min(left if last else share(taxable, tax, all_taxable), left)
        left -= part
        out.append(Lot(pre + part, part, taxable, ev.lot_location, key))
    return out


def order_taxable_shares(order_pks):
    """Order pk -> taxable part of the order's pre-tax amount (lines with a tax rate: qty × exact unit price, half-up,
    less any receipt-discount share — the same net extension the receipts use)."""
    from sales.models import line_amount
    taxable = {}
    for doc_id, qty, price, rate, recorded, share_ in Line.objects.filter(document_id__in=order_pks).values_list(
            'document_id', 'quantity', 'price', 'tax_rate', 'recorded_amount', 'receipt_discount'):
        if rate and rate > 0:
            taxable[doc_id] = taxable.get(doc_id, ZERO) + line_amount(qty, price, recorded) - (share_ or ZERO)
    return taxable


def order_taxable(order, amount, tax, shares):
    """Taxable base inside `amount` (incl. `tax`) paid on an order, from the order's own taxable share."""
    pretax = money(order.total or ZERO) - money(order.tax or ZERO)
    if not order.tax or not pretax:
        return ZERO
    return money((amount - tax) * min(shares.get(order.pk, pretax), pretax) / pretax)


def customer_exempt(customer, fallback):
    return (is_exempt_location(customer.tax_location) or customer.tax_exempt) if customer else fallback


def order_charge_keys(order):
    """The ledger keys of the Account charges an order's payments made: its QuickBooks deposit receipts paid by
    Account (('sale', pk)) and its app payments by Account (('order_payment', pk)). Used by a settlement adjustment
    to find the customer's open charges for the order (`Ledger.adjust_charge`)."""
    keys = {('sale', pk) for pk in Document.objects.filter(deposit_order=order, kind='sale', status='completed', receipt_type__in=(2, 3))
            .values_list('pk', flat=True)}
    keys |= {('order_payment', pk) for pk in OrderPayment.objects.filter(order=order, method='account', adjustment=False).values_list('pk', flat=True)}
    return keys


def unknown_debt(customer, events):
    """What QuickBooks says this customer owed at the backup beyond what the imported receipts explain: debt from
    before the data (an opening balance, an adjustment). Positive, it is seeded as the oldest lot so payments settle
    it first at the 7/107 estimate, and so a payment beyond the known charges is only called a prepayment when
    QuickBooks agrees nothing older is owed. Negative (QuickBooks' balance is below the receipts), it is an
    adjustment QuickBooks made with no receipt in the data — applied at the backup moment by `Ledger.reconcile`:
    what imported charges still show as owed is settled first (no money, no tax), and only the rest is credit
    QuickBooks holds for the customer. Zero for customers made in this app."""
    if customer is None:
        return ZERO
    explained = sum((ev.charged - ev.payment for ev in events if ev.kind != 'order_payment' and ev.doc.from_quickbooks), ZERO)
    return money(customer.imported_account_balance or ZERO) - explained


def _parse(raw_events, walk, lower, upper):
    """Turn the sorted raw events into Events (tenders, lines, kinds, amounts) — everything that doesn't need the
    ledger. `in_period` marks the ones inside [lower, upper)."""
    events = []
    for when, kind, _, obj in raw_events:
        in_period = lower is None or lower <= when < upper
        if kind == 0:
            doc = obj
            if doc.receipt_type == 4:
                if in_period:
                    walk.excluded['payout'] += 1
                continue
            if not doc.from_quickbooks and doc.source_order_id:
                if in_period:
                    walk.excluded['pickup'] += 1
                continue
            order = doc.deposit_order if doc.receipt_type in (2, 3) else None
            if order and (order.deposit_used or ZERO) > 0:
                if in_period:
                    walk.excluded['applied'] += 1
                    walk.applied_total += money(doc.total or ZERO)
                continue
            tender, missing = tenders(doc)
            if in_period and missing:
                walk.excluded['missing'] += 1
            if in_period and getattr(doc, '_derived_tender', False):
                walk.excluded['derived'] += 1
            lines = list(doc.lines.all())
            is_deposit = doc.receipt_type in (2, 3)
            ev = Event(when, 'deposit' if is_deposit else 'receipt', doc, order=order, tender=tender, lines=lines,
                       is_deposit=is_deposit, in_period=in_period, excluded='missing' if missing else '')
            ev.poa_amount = sum((line.amount for line in lines if line.item_name == POA_NAME), ZERO)
            ev.poa = bool(lines) and all(line.item_name == POA_NAME for line in lines)
            if ev.poa:
                ev.kind = 'poa'
            ev.tax_location = location(order) if order else location(doc)
            if order and doc.tax_location:
                ev.tax_location = doc.tax_location
            if order:
                ev.lock = doc.tax_lock
        else:
            order = obj.order
            tender = dict.fromkeys(TENDERS, ZERO)
            tender[tender_key(obj.method)] = money(obj.amount)
            ev = Event(when, 'order_payment', order, order=order, source=obj, tender=tender, is_deposit=True,
                       in_period=in_period, tax_location=location(order), lock=obj.tax_lock, adjustment=obj.adjustment)
            doc = order
        if ev.lock is not None:
            ev.tax_location = ev.lock.location    # the section it was reported in on its day
        ev.gift = tender['gift']
        ev.received = sum((tender[k] for k in MONEY_IN), ZERO)
        ev.money_in = ev.received + ev.gift
        ev.account = tender['account']
        ev.new_sale = money(doc.total or ZERO) - ev.poa_amount
        ev.lot_location = section_of(ev.tax_location, ev.target.tax_exempt)
        if ev.is_deposit:
            ev.charged = ev.account if order else ZERO
        else:
            ev.mixed = bool(ev.poa_amount) and not ev.poa
            if ev.mixed:
                # A return funds an account credit, not a cash payment. Any POA
                # beyond the return value is an actual payment (e.g. a net-paid mix).
                ev.credit = min(max(ev.poa_amount, ZERO), max(-ev.new_sale, ZERO))
                ev.payment = ev.poa_amount - ev.credit
            elif ev.poa:
                ev.payment = ev.money_in
            ev.charged = ev.account - ev.credit
        events.append(ev)
    return events


def replay(raw_events, lower=None, upper=None):
    """Run the ledger over events sorted by time; returns a Walk whose `events` are those inside [lower, upper).
    Events at or after `upper` are parsed (they tell what QuickBooks' balance explains, see `unknown_debt`) but never
    replayed, so a period's figures don't change when later documents arrive."""
    raw_events.sort(key=lambda e: e[:3])
    ledgers = defaultdict(Ledger)
    orders = defaultdict(lambda: [ZERO, ZERO])     # order pk -> [paid so far, tax so far]
    walk = Walk([], defaultdict(int), ledgers=ledgers)
    events = _parse(raw_events, walk, lower, upper)
    by_customer = defaultdict(list)
    for ev in events:
        if ev.target.customer_id is not None:
            by_customer[ev.target.customer_id].append(ev)
    for customer_id, customer_events_ in by_customer.items():
        customer = customer_events_[0].target.customer
        debt = unknown_debt(customer, customer_events_)
        context = estimate_context(customer_events_[0].doc)
        if debt > 0:
            ledgers[customer_id].lots.append(estimated_lot(debt, customer_exempt(customer, False), key=None, qb=True, context=context))
        elif debt < 0:
            # Applied at the backup moment: after the customer's last imported receipt (before everything, with none).
            imported = [ev for ev in customer_events_ if ev.kind != 'order_payment' and ev.doc.from_quickbooks]
            if imported:
                imported[-1].reconcile = -debt
            else:
                ledgers[customer_id].reconcile(-debt, customer_exempt(customer, False), context)
    shares = {}
    for ev in events:
        when, doc, order, target = ev.when, ev.doc, ev.order, ev.target
        in_period = ev.in_period
        if upper is not None and when >= upper:
            break
        customer_id = target.customer_id
        ledger = ledgers[customer_id] if customer_id is not None else Ledger()
        if ev.is_deposit:
            if not order:
                ev.excluded = 'unlinked'
                if in_period:
                    walk.excluded['unlinked'] += 1
            else:
                # Cumulative share so the order's payments add up to exactly its tax; the Account part is deferred.
                paid, taxed = orders[order.pk]
                before = paid
                paid += ev.money_in + ev.account
                order_total, order_tax = money(order.total or ZERO), money(order.tax or ZERO)
                if ev.lock is not None:
                    # the figures stored on the day the money was received; the order may have changed since.
                    payment_tax = ev.lock.tax
                    if ev.adjustment and tender_key(ev.source.method) == 'account':
                        # A settlement adjustment of the deferred (Account) part: nothing collected today.
                        ev.merchandise_tax, ev.deferred = ZERO, payment_tax
                    else:
                        ev.merchandise_tax, ev.deferred = split_payment_tax(ev.money_in, ev.account, payment_tax)
                    ev.account_taxable = ev.lock.account_taxable
                    ev.taxable = ev.lock.taxable - ev.account_taxable
                else:
                    # Not locked yet (before `lock_order_payment_tax`): the order as it stands now.
                    # CAP: payments beyond the order's total carry no tax (the excess is a deposit balance, not a sale);
                    # a later refund of that excess carries none either. The spreadsheet comparison would tax it at 7/107.
                    payment_tax = cumulative_order_tax(paid, order_tax, order_total) - taxed
                    if order.pk not in shares:
                        shares.update(order_taxable_shares([order.pk]))
                        shares.setdefault(order.pk, ZERO)
                    ev.merchandise_tax, ev.deferred, ev.taxable, ev.account_taxable = order_payment_figures(order, ev.money_in, ev.account, payment_tax, shares)
                orders[order.pk] = [paid, taxed + payment_tax]
                ev.capped = bool(order_total) and (paid > order_total or before > order_total)
                if ev.account:
                    key = ('sale', doc.pk) if ev.kind != 'order_payment' else ('order_payment', ev.source.pk)
                    ev.adjustments = ledger.charge(Lot(ev.account, ev.deferred, ev.account_taxable, ev.lot_location, key,
                                                       qb=ev.kind != 'order_payment' and doc.from_quickbooks))
                elif ev.adjustment and (ev.deferred or ev.account_taxable):
                    # (F3): the deferred part of a settlement adjustment goes on the customer's still-open
                    # Account charges for this order (which follow the order's section now); the share of the charge
                    # already paid released its tax when it was paid, so that share is counted today instead.
                    ev.adjustments = ledger.adjust_charge(order_charge_keys(order), ev.deferred, ev.account_taxable, ev.lot_location)
            ev.reconciled = sum((a.tax for a in ev.adjustments), ZERO)
            ev.tax = ev.merchandise_tax + ev.reconciled
        else:
            qb = doc.from_quickbooks
            ev.merchandise_tax = share(ev.money_in, doc.tax, doc.total)
            if ev.mixed:
                ev.merchandise_tax = share(ev.money_in - ev.payment, doc.tax, ev.new_sale)
                ev.deferred = money(doc.tax or ZERO) - ev.merchandise_tax
                ev.adjustments = ledger.charge(Lot(ev.charged, ev.deferred, charged_taxable(ev, ev.charged, ev.deferred), ev.lot_location, ('sale', doc.pk), qb=qb),
                                               returned_groups(ev) if ev.credit else ())
                ev.cancelled = ledger.cancelled
            elif ev.poa:
                ev.merchandise_tax = ZERO
            elif ev.account:
                # A negative Account tender is the app's own Accept Return refunded to the account (QuickBooks wrote
                # such returns as a Payment On Account line, the `mixed` branch): the same linked-return rule applies,
                # so merchandise still owed comes off its own charge with no tax row (review 4, finding 1).
                ev.deferred = (money(doc.tax or ZERO) - ev.merchandise_tax) if doc.total else ZERO
                ev.adjustments = ledger.charge(Lot(ev.account, ev.deferred, charged_taxable(ev, ev.account, ev.deferred), ev.lot_location, ('sale', doc.pk), qb=qb),
                                               returned_groups(ev) if ev.account < 0 else ())
                ev.cancelled = ledger.cancelled
            if ev.poa or (ev.mixed and ev.payment):
                ev.customer_exempt = customer_exempt(doc.customer, is_exempt_location(ev.tax_location))
                ev.allocations = ledger.pay(ev.payment, ev.customer_exempt, estimate_context(doc))
                ev.adjustments = ev.adjustments + ledger.adjustments
                ev.payment_tax = sum((a.tax for a in ev.allocations), ZERO)
                ev.estimated = sum((a.amount for a in ev.allocations if a.estimated), ZERO)
                ev.estimated_tax = sum((a.tax for a in ev.allocations if a.estimated), ZERO)
            ev.reconciled = sum((a.tax for a in ev.adjustments), ZERO)
            ev.tax = ev.merchandise_tax + ev.payment_tax + ev.reconciled
        if ev.reconcile:
            ledger.reconcile(ev.reconcile, customer_exempt(target.customer, False), estimate_context(doc))
        # Exempt money received: by the paid charges' locations for a payment on account, the receipt's otherwise;
        # a true-up moves the money a credit carried to the location of the charge it really bought.
        merchandise = ev.received - min(ev.payment, ev.received) if ev.mixed else ev.received
        ev.exempt_received = sum((a.amount for a in ev.allocations if is_exempt_location(a.location)), ZERO) if ev.payment else ZERO
        ev.exempt_received += sum((a.amount for a in ev.adjustments if is_exempt_location(a.location)), ZERO)
        if is_exempt_location(ev.lot_location) and not ev.poa:
            ev.exempt_received += merchandise
        if not in_period:
            continue
        walk.events.append(ev)
        if ev.estimated:
            if ev.customer_exempt:
                walk.exempt_estimated_count += 1
            else:
                walk.estimated_count += 1
            walk.estimated_total += ev.estimated
            walk.estimated_tax += ev.estimated_tax
        if ev.account > 0 and not ev.excluded:
            walk.deferred_total += ev.account
            walk.deferred_tax += ev.deferred
        if ev.payment:
            walk.poa_total += ev.payment
            walk.poa_tax += ev.payment_tax
        if ev.reconciled:
            walk.poa_tax += ev.reconciled
            walk.reconciled_tax += ev.reconciled
        if ev.cancelled:
            walk.cancelled_total += sum((c.amount for c in ev.cancelled), ZERO)
            walk.cancelled_tax += sum((c.tax for c in ev.cancelled), ZERO)
        if ev.gift and not ev.excluded:
            walk.gift_total += ev.gift
            walk.gift_tax += share(ev.gift, ev.merchandise_tax, ev.money_in) if ev.money_in else ZERO
    return walk


def walk(period):
    lower, upper = bounds(period.start, period.end)
    return replay(period_events(lower, upper), lower, upper)


# ----: what is still owed per charge, from the same FIFO state --------------------------------------------

def open_charges(customer):
    """Charges still owed by this customer, oldest first: [(key, charged, owed)] with key ('sale', pk) or
    ('order_payment', pk), one entry per charge (a charge reopened by several refunds is one entry owing the sum).
    FIFO means what is still owed is the newest charges; the list is capped at the customer's account balance (older
    charges drop first) so a balance QuickBooks already settled never shows a receipt as owed. Nothing is stored."""
    balance = customer.account_balance
    if balance <= 0:
        return []       # nothing can be owed; skip the replay (the house accounts have thousands of receipts)
    walk_ = replay(customer_events(customer.pk))
    ledger = walk_.ledgers.get(customer.pk)
    if ledger is None:
        return []
    charged, order = {}, {}
    for position, ev in enumerate(walk_.events):
        if ev.account > 0:
            k = ('order_payment', ev.source.pk) if ev.kind == 'order_payment' else ('sale', ev.doc.pk)
            charged[k] = charged.get(k, ZERO) + ev.account
            order.setdefault(k, position)
    open_ = {}
    for lot in ledger.lots:
        if lot.key is not None and lot.amount > 0:
            open_[lot.key] = open_.get(lot.key, ZERO) + lot.amount
    lots = sorted(open_.items(), key=lambda kv: order.get(kv[0], -1))
    excess = sum((amount for _, amount in lots), ZERO) - balance
    owed = []
    for key, amount in lots:
        if excess > 0:
            cut = min(excess, amount)
            amount -= cut
            excess -= cut
        if amount > 0:
            owed.append((key, charged.get(key, amount), amount))
    return owed


def owed_by_receipt(documents):
    """{receipt pk: amount still owed} for the receipts in `documents` that charged something to an account."""
    by_customer = defaultdict(list)
    for doc in documents:
        if doc.kind != 'sale' or doc.status != 'completed' or doc.customer_id is None:
            continue
        if doc.from_quickbooks:
            charged = any(t.method == 'account' and not t.use_in_total and t.amount and t.amount > 0 for t in doc.tenders.all())
        else:
            charged = any(p.get('method') == 'account' and Decimal(str(p.get('amount') or 0)) > 0 for p in doc.payment_breakdown or [])
        if charged:
            by_customer[doc.customer_id].append(doc)
    owed = {}
    for customer_id, docs in by_customer.items():
        customer = docs[0].customer
        for key, _charged, amount in open_charges(customer):
            if key[0] == 'sale':
                owed[key[1]] = amount
    return {doc.pk: owed[doc.pk] for docs in by_customer.values() for doc in docs if doc.pk in owed}
