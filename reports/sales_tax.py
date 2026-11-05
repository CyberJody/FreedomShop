# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sales Tax report: money received in a period, split by QB POS tax location, on a shop's cash basis.

Sections: one per tax location in Tax Settings — the default (taxable) location first, any other active taxable ones,
the exempt location last (e.g. "Main Street" and "Exempt") — each with a summary line per
part — Sales receipts, Customer order payments, Payments on account — and, in the detailed report, one row per
document. The timing and tax rules come from `tax_ledger`, the same ledger Daily Totals uses, so the two reports
tie to the cent.

Per sales receipt (whole cents, rounded half-up like QB):
- Total = money in on the sale day (every tender except Account; the Account part waits until it is paid)
- Sales Tax = receipt tax × money in ÷ receipt total (the whole recorded tax when the receipt is paid in full)
- Gross Sales = receipt SubTotal as QB recorded it (item lines before any receipt-level discount), scaled to the
                 pre-tax share received; Discount = the receipt-level discount, scaled the same way
- Taxable = item lines whose tax rate > 0 (qty × exact unit price, half-up) less each line's share of the
                 receipt discount (QB takes it off before computing tax; the whole discount on imported receipts not
                 yet backfilled with per-line shares), scaled; Non-taxable = Gross − Discount − Taxable
A receipt paid in full shows exactly QB's recorded figures. Reversals (type 1) carry negative amounts. Payouts
(type 4) and "Payment On Account" lines are not sales.

Payments on account: the payment is split by the charges it pays (oldest first), each part under the location of
the charge it settles (a customer who has since become Exempt still releases the tax on an older taxable
charge) with that charge's own taxable / non-taxable bases and the tax it carried; a payment covering taxable and
exempt charges appears in both sections. Only money no known charge explains (debt from before the backup, or a
prepayment held as credit for the customer's next charge) is estimated: exactly r/(100 + r) — 7/107 at 7 % — of a taxable customer's
(all taxable) and zero for Exempt (all non-taxable), flagged "estimated". When a credit (a prepayment, or a return
credited to the account) later funds a charge, a "tax trued up" row per location replaces the credit's tax and bases
with the charge's own on the charge's day — the rows' Totals net to zero since no money moves. A return of merchandise
still owed on the account (its receipt's charge unpaid) comes off that charge instead: no tax was collected on it, so
none is counted or reversed. Refunds hand back unapplied credit first, then reopen the newest paid charges.

Customer order payments: each QB deposit (type 2) / refund (type 3) receipt and each payment recorded
on an order in this app counts on its own date and location: Sales Tax = payment × order tax ÷ order total, worked
cumulatively so the payments add up to exactly the order's tax; Gross = payment − tax; Taxable = Gross × the order's
taxable share. these figures are locked on the payment's day (stored on the receipt / payment by
`tax_ledger.lock_payment` and the `lock_order_payment_tax` backfill); an edit to the order afterwards moves only the
tax still due, booked on the next payment. A deposit paid by Account is a charge (its tax waits). Not counted (listed in the notes): deposits with
no order in the backup, deposits on 2008–09 orders later picked up on a receipt (QB taxed the receipt), and pickup
receipts made in this app from a paid order (taxed with the order payments).
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from . import tax_ledger
from .tax_ledger import ZERO, POA_NAME  # noqa: F401 (POA_NAME re-exported)
from sales.models import rate_text
from shop_settings.config import tax as shop_tax

CENT = Decimal('0.01')


def section_order():
    """Every tax location name in report order: the default location, other active taxable ones, the exempt one, then
    retired locations (still named on older documents)."""
    config = shop_tax()
    names = [loc.name for loc in config.sections]
    return names + [loc.name for loc in config.locations if loc.name not in names]


def section_title(location):
    if location.is_exempt:
        return f'{location.section_label} — No Sales Tax'
    return f'{location.section_label} — Sales Tax {rate_text(location.rate)}%'


def cents(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def _bounds(start, end):
    return tax_ledger.bounds(start, end)


@dataclass
class Row:
    date: date
    receipt: str
    customer: str
    type_label: str
    gross: Decimal
    discount: Decimal
    nontaxable: Decimal
    taxable: Decimal
    tax: Decimal
    total: Decimal
    kind: str = 'sale'      # 'sale' (sales receipt), 'order' (customer order payment) or 'account' (payment on account)


PART_LABELS = {'sale': 'Sales receipts', 'order': 'Customer order payments', 'account': 'Payments on account'}
AMOUNTS = ('gross', 'discount', 'nontaxable', 'taxable', 'tax', 'total')


@dataclass
class Part:
    label: str
    receipts: int = 0
    gross: Decimal = ZERO
    discount: Decimal = ZERO
    nontaxable: Decimal = ZERO
    taxable: Decimal = ZERO
    tax: Decimal = ZERO
    total: Decimal = ZERO


@dataclass
class Section:
    location: str
    title: str
    rows: list = field(default_factory=list)
    receipts: int = 0
    gross: Decimal = ZERO
    discount: Decimal = ZERO
    nontaxable: Decimal = ZERO
    taxable: Decimal = ZERO
    tax: Decimal = ZERO
    total: Decimal = ZERO

    exempt: bool = False

    @property
    def taxed(self):
        return not self.exempt

    parts: dict = field(default_factory=lambda: {k: Part(v) for k, v in PART_LABELS.items()})

    def add(self, row):
        self.rows.append(row)
        self.receipts += 1
        part = self.parts[row.kind]
        part.receipts += 1
        for name in AMOUNTS:
            setattr(self, name, getattr(self, name) + getattr(row, name))
            setattr(part, name, getattr(part, name) + getattr(row, name))

    @property
    def part_list(self):
        return [self.parts[k] for k in PART_LABELS]


@dataclass
class Report:
    period: object
    detailed: bool
    sections: list
    deposits_count: int
    deposits_total: Decimal
    payments_on_account: Decimal
    other_locations: int
    unlinked_deposits: int = 0
    unlinked_total: Decimal = ZERO
    applied_deposits: int = 0
    applied_total: Decimal = ZERO
    native_pickups: int = 0
    payments_on_account_tax: Decimal = ZERO
    charged_to_account: Decimal = ZERO
    charged_to_account_tax: Decimal = ZERO
    estimated_count: int = 0
    estimated_total: Decimal = ZERO
    estimated_tax: Decimal = ZERO
    reconciled_tax: Decimal = ZERO
    gift_total: Decimal = ZERO
    gift_tax: Decimal = ZERO
    missing_tenders: int = 0
    derived_tenders: int = 0

    title = 'Sales Tax'

    @property
    def subtitle(self):
        return f"{'Detail' if self.detailed else 'Summary'} · {self.period.label}"

    @property
    def receipts(self):
        return sum(s.receipts for s in self.sections)

    def _sum(self, name):
        return sum((getattr(s, name) for s in self.sections), ZERO)

    gross = property(lambda self: self._sum('gross'))
    discount = property(lambda self: self._sum('discount'))
    nontaxable = property(lambda self: self._sum('nontaxable'))
    taxable = property(lambda self: self._sum('taxable'))
    tax = property(lambda self: self._sum('tax'))
    total = property(lambda self: self._sum('total'))

    def part(self, kind):
        """All-locations totals for 'sale', 'order' or 'account'."""
        out = Part(PART_LABELS[kind])
        for s in self.sections:
            p = s.parts[kind]
            out.receipts += p.receipts
            for name in AMOUNTS:
                setattr(out, name, getattr(out, name) + getattr(p, name))
        return out

    @property
    def part_totals(self):
        return [self.part(k) for k in PART_LABELS]


def build_daily(period):
    """Daily cash-basis view of the same ledger (own page under Reports)."""
    from .daily_totals import build as daily_build
    return daily_build(period)


def _number(doc):
    return str(doc.source_number) if doc.source_number is not None else (str(doc.number_value) if doc.number_value is not None else '')


def _receipt_figures(ev):
    """(gross, discount, taxable, nontaxable) for the money-in share of a sales receipt (the ledger's own figures,
    so the charged part's bases on the ledger and this row add up to the receipt)."""
    return tax_ledger.receipt_figures(ev)


def _order_figures(ev):
    """(gross, taxable, nontaxable) of an order payment's money-in share: the ledger's figures for the payment (locked
    on the day it was received)."""
    gross = ev.money_in - ev.merchandise_tax
    return gross, ev.taxable, gross - ev.taxable


def _payment_rows(when, number, customer, ev):
    """One row per report section for a payment on account: the payment split by the charges it settled, each part
    reported under that charge's own tax location with the charged taxable / non-taxable bases (residual cents kept,
    the closing payment takes the remainder). Only the part matched by no known charge is estimated — exactly 7/107 of
    it taxable for a taxable customer, all non-taxable for an exempt one — and labelled so."""
    out = []
    for location in section_order():
        parts = [a for a in ev.allocations if a.location == location]
        if not parts:
            continue
        total = sum((a.amount for a in parts), ZERO)
        tax = sum((a.tax for a in parts), ZERO)
        taxable = sum((a.taxable for a in parts), ZERO)
        if not total and not tax:
            continue
        label = 'Payment on account' + (' · estimated' if any(a.estimated for a in parts) else '')
        out.append((location, Row(when, number, customer, label, total - tax, ZERO, total - tax - taxable, taxable, tax, total, kind='account')))
    return out


TRUE_UP_LABELS = {'prepaid': 'Prepayment applied · tax trued up', 'return': 'Return credit applied · tax trued up',
                  'cancelled': 'Return against charge · tax trued up', 'adjustment': 'Sales tax adjustment · charge already paid'}


def _true_up(sections, when, number, customer, ev):
    """A charge funded by a credit (a prepayment taxed at the 7/107 estimate when its money came in, or a return
    credited to the account carrying the returned merchandise's tax): now the charge says what that money bought.
    One row per location: the credit's tax and bases reversed where they were reported, the charge's own tax and
    bases where it belongs — no money moves (the rows' Totals net to zero), and every row still adds up
    (Gross − Discount = Taxable + Non-taxable; + Tax = Total)."""
    for location in section_order():
        parts = [a for a in ev.adjustments if a.location == location]
        if not parts:
            continue
        total = sum((a.amount for a in parts), ZERO)
        tax = sum((a.tax for a in parts), ZERO)
        taxable = sum((a.taxable for a in parts), ZERO)
        if not total and not tax and not taxable:
            continue
        kinds = {a.kind for a in parts}
        label = TRUE_UP_LABELS[kinds.pop()] if len(kinds) == 1 else 'Credit applied · tax trued up'
        sections[location].add(Row(when, number, customer, label, total - tax, ZERO, total - tax - taxable, taxable, tax, total, kind='account'))


def build(period, detailed=False):
    from .tax_periods import configured_start_month
    configured_start_month()
    walk = tax_ledger.walk(period)
    config = shop_tax()
    sections = {}
    for name in section_order():
        location = config.location(name)
        sections[name] = Section(name, section_title(location), exempt=location.is_exempt)
    report = Report(period, detailed, [sections[location.name] for location in config.sections], walk.excluded['payout'], ZERO, walk.poa_total, 0,
                    native_pickups=walk.excluded['pickup'], applied_deposits=walk.excluded['applied'], applied_total=walk.applied_total,
                    payments_on_account_tax=walk.poa_tax, charged_to_account=walk.deferred_total, charged_to_account_tax=walk.deferred_tax,
                    estimated_count=walk.estimated_count + walk.exempt_estimated_count, estimated_total=walk.estimated_total,
                    estimated_tax=walk.estimated_tax, reconciled_tax=walk.reconciled_tax, gift_total=walk.gift_total, gift_tax=walk.gift_tax, missing_tenders=walk.excluded['missing'],
                    derived_tenders=walk.excluded['derived'])
    if report.deposits_count:
        from django.db.models import Sum
        from sales.models import Document
        lower, upper = _bounds(period.start, period.end)
        report.deposits_total = cents(Document.objects.filter(kind='sale', status='completed', receipt_type=4, completed_at__gte=lower,
                                                              completed_at__lt=upper).aggregate(t=Sum('total'))['t'])
    for ev in walk.events:
        doc = ev.doc
        when = ev.day
        if ev.excluded == 'unlinked':
            report.unlinked_deposits += 1
            report.unlinked_total += ev.money_in
            continue
        section = sections.get(ev.tax_location)
        if section is None:
            report.other_locations += 1
            section = sections[config.location_for(ev.target.tax_exempt)]
        if ev.kind == 'order_payment':
            order = ev.order
            if ev.adjustment:
                label = f'Sales tax adjustment · {order.numbered_label}'   # (F3): no money moved
            else:
                label = f"{'Order refund' if ev.source.amount < 0 else 'Order payment'} · {order.numbered_label}"
            if ev.account:
                label += ' · On account'
            gross, taxable, nontaxable = _order_figures(ev)
            section.add(Row(when, f'{order.number} pmt', order.customer_name, label, gross, ZERO, nontaxable, taxable, ev.merchandise_tax, ev.money_in, kind='order'))
            _true_up(sections, when, f'{order.number} pmt', order.customer_name, ev)
            continue
        if ev.kind == 'deposit':
            order = ev.order
            label = f"{'Deposit refund' if doc.receipt_type == 3 else 'Order deposit'} · {order.numbered_label}"
            if ev.account:
                label += ' · On account'
            gross, taxable, nontaxable = _order_figures(ev)
            section.add(Row(when, doc.number, doc.customer_name, label, gross, ZERO, nontaxable, taxable, ev.merchandise_tax, ev.money_in, kind='order'))
            _true_up(sections, when, doc.number, doc.customer_name, ev)
            continue
        number = _number(doc)
        if ev.kind == 'poa':
            for location, row in _payment_rows(when, number, doc.customer_name, ev):
                sections[location].add(row)
            _true_up(sections, when, number, doc.customer_name, ev)
            continue
        type_label = {1: 'Reversal'}.get(doc.receipt_type, '')
        if ev.account:
            type_label = (type_label + ' · ' if type_label else '') + 'On account'
        if ev.credit:
            type_label = (type_label + ' · ' if type_label else '') + f'Credited to account ${ev.credit:,.2f}'
        gross, discount, taxable, nontaxable = _receipt_figures(ev)
        section.add(Row(when, number, doc.customer_name, type_label, gross, discount, nontaxable, taxable, ev.merchandise_tax, ev.money_in - ev.payment))
        _true_up(sections, when, number, doc.customer_name, ev)
        if ev.mixed and ev.payment:
            for location, row in _payment_rows(when, number, doc.customer_name, ev):
                sections[location].add(row)
    for section in sections.values():
        section.rows.sort(key=lambda row: (row.date, row.kind != 'sale'))
        if section not in report.sections and (section.rows or section.receipts):
            report.sections.append(section)   # a retired location that still has money in this period
    return report
