# SPDX-License-Identifier: AGPL-3.0-or-later
from config.provenance import imported_source, native_source
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from shop_settings.config import default_estimate_location, default_estimate_rate


def store_today():
    """Today's store date (America/New_York, settings.TIME_ZONE). One place so tests can pin the day."""
    return timezone.localdate()


def money(value):
    """QuickBooks cents: round half-up, including refunds ($106.475 -> $106.48; -$14.955 -> -$14.96)."""
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


UNIT_PRICE_PLACES = 4


def unit_price(value):
    """A unit price as QuickBooks keeps it: up to 4 decimals (a % line discount leaves 1,379.95 x 90 % = 1241.9550),
    rounded half-up at the 4th place. Typed prices are whole cents (services.decimal rejects more); only discounts
    create the extra decimals. Shown everywhere to 2 decimals (`plain`)."""
    return Decimal(value).quantize(Decimal(1).scaleb(-UNIT_PRICE_PLACES), rounding=ROUND_HALF_UP)


def discounted_price(regular, percent):
    """QB's line % discount: the exact discounted unit price, kept to 4 decimals (169.95 at 25 % -> 127.4625).
    Verified on a shop's receipts since 9/2021: 593 of 594 % lines store exactly this (the one miss is a
    41.09 % line QB kept at 6 decimals; its line total is the same)."""
    return unit_price(Decimal(regular) * (Decimal('100') - Decimal(percent)) / Decimal('100'))


def allocate_receipt_discount(amounts, percent, total=None):
    """QB's receipt-level % discount, spread over the eligible lines. `amounts` are the lines' extensions (gross, whole cents); returns the per-line discount amounts.
      discount total = half-up(sum of the extensions x percent) [or `total` when QB recorded a typed dollar amount]
      per line = half-up(extension x percent)
      leftover cent = on the line with the LARGEST extension (first one on ties)
    Rule decided by a shop's receipts (10/9 data, every %-discount receipt since 9/1/2021 recomputed through this
    code: 246 of 249 exact; the 3 misses are even exchanges (-n and +n of one item) where QB stored no shares at all;
    the "last line" alternative fails on dozens). Verified by import parity checks."""
    amounts = [Decimal(a) for a in amounts]
    percent = Decimal(percent)
    if not amounts or (not percent and not total):
        return [Decimal('0.00')] * len(amounts)
    if total is None:
        total = money(sum(amounts, Decimal('0')) * percent / 100)
    if percent:
        shares = [money(amount * percent / 100) for amount in amounts]
    else:
        # A dollar amount with no percent ($ order discount): each line's share is its part of the amount by
        # extension, half-up; QB stores a typed dollar amount as a 6-decimal percent and spreads from that, which
        # gives the same shares on every typed-dollar receipt in a shop's data.
        base = sum(amounts, Decimal('0'))
        shares = [money(Decimal(total) * amount / base) if base else Decimal('0.00') for amount in amounts]
    leftover = Decimal(total) - sum(shares, Decimal('0'))
    if leftover:
        largest = max(range(len(amounts)), key=lambda i: (amounts[i], -i))
        shares[largest] += leftover
    return shares


def tax_amount(lines, exempt=False):
    """Sales tax like QB POS: per tax rate on that rate's taxable amount, not line by line; cents rounded half-up once.
    `lines` yields (amount, rate) pairs; pass the exact (unrounded) line amounts (Line.tax_basis) — QB taxes
    129.95 + 109.975 as 239.925 (tax 16.79), not the cent-rounded 239.93 (16.80)."""
    if exempt:
        return Decimal('0.00')
    by_rate = {}
    for amount, rate in lines:
        if rate:
            by_rate[Decimal(rate)] = by_rate.get(Decimal(rate), Decimal('0')) + Decimal(amount)
    return sum((money(amount * rate / 100) for rate, amount in by_rate.items()), Decimal('0.00'))


def line_amount(quantity, price, recorded=None):
    """A line's extension: qty x the exact unit price (up to 4 decimals), rounded half-up to whole cents, as QB does
    (4 x 314.955 = 1,259.82). `recorded` is QuickBooks' own line total, kept only on imported lines whose QB price had
    more than 4 decimals, so those old receipts still add up."""
    return recorded if recorded is not None else money(Decimal(quantity) * Decimal(price))


def qb_line_amount(quantity, raw_price, stored_price):
    """What QB recorded for an imported line (qty x its exact price, rounded to the cent as QB did), or None when
    that equals the extension of the stored (4-decimal) price anyway."""
    if quantity is None or raw_price is None or stored_price is None:
        return None
    recorded = money(Decimal(quantity) * Decimal(raw_price))
    return None if recorded == money(Decimal(quantity) * Decimal(stored_price)) else recorded


def plain(value):
    """Decimal text for forms and JSON: always whole cents (a 4-decimal unit price shows as 2 decimals)."""
    if value is None:
        return ''
    return str(money(value))


def exact_text(value):
    """A unit price's exact text for the register's arithmetic ('127.4625', '90.00'); never shown as is."""
    if value is None:
        return ''
    text = f'{Decimal(value):f}'
    if '.' in text:
        text = text.rstrip('0')
        whole, _, fraction = text.partition('.')
        text = f'{whole}.{fraction.ljust(2, "0")}'
    else:
        text += '.00'
    return text


def quantity_text(value):
    """Count text with no needless decimals (quantities are whole numbers): 5.000 -> '5', 0 -> '0'.
    The three imported QB typo lines with fractional quantities keep their fraction, trailing zeros dropped (60.770 -> '60.77')."""
    if value is None:
        return ''
    text = f'{Decimal(value):f}'
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return '0' if text in ('', '-0') else text


def rate_text(value):
    """Tax rate (%) text without needless decimals: 7.000 -> '7', 8.875 -> '8.875' (same rule as quantity_text)."""
    return quantity_text(value)


# Accept Return reason pick-list. Stored values (left) never change once used; edit the labels or add
# entries here. Shown on the register, the printed return receipt and in Sales History.
RETURN_REASONS = [
    ('defective', 'Defective/Damaged'),
    ('changed_mind', 'Changed mind'),
    ('wrong_item', 'Wrong item'),
    ('exchange', 'Exchange'),
    ('other', 'Other'),
]


# Optional restocking fee on Accept Return: the cashier may tick "Apply 20% restocking fee"; never automatic.
# The one place the rate lives (the printed receipt footer wording is separate and not changed here).
RESTOCKING_FEE_PERCENT = Decimal('20')
RESTOCKING_FEE_NAME = 'Restocking fee'


def restocking_fees(returned):
    """Fee lines for returned lines: `returned` yields (line amount, tax rate) of the return lines (negative amounts).
    One fee per tax rate = RESTOCKING_FEE_PERCENT of the returned merchandise at that rate, rounded half-up to whole cents,
    taxed at that same rate (a shop's QB data rang its "Restocking fee" item as taxable at 7%). Returns
    [(fee, rate)] with fee > 0. The register JS (computeFees) mirrors this."""
    by_rate = {}
    for amount, rate in returned:
        key = Decimal(rate) if rate is not None else None
        by_rate[key] = by_rate.get(key, Decimal('0')) - Decimal(amount)
    fees = [(money(base * RESTOCKING_FEE_PERCENT / 100), rate) for rate, base in by_rate.items()]
    return [(fee, rate) for fee, rate in fees if fee > 0]


class MoneyField(models.DecimalField):
    """Dollars and cents. Values are explicitly rounded half-up to whole cents when saved, so no
    fraction of a cent can be stored however it was computed. Migrations see a plain DecimalField."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 16)
        kwargs.setdefault('decimal_places', 2)
        super().__init__(*args, **kwargs)

    def get_db_prep_save(self, value, connection):
        if value is not None and not hasattr(value, 'resolve_expression'):
            value = money(self.to_python(value))
        return super().get_db_prep_save(value, connection)

    def deconstruct(self):
        name, _, args, kwargs = super().deconstruct()
        return name, 'django.db.models.DecimalField', args, kwargs


class PriceField(models.DecimalField):
    """A unit price: up to 4 decimals, rounded half-up at the 4th place when saved (QB keeps % line discounts
    exactly: 1241.9550). Only Line.price uses it; every other money column stays a 2-place MoneyField.
    Migrations see a plain DecimalField."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 16)
        kwargs.setdefault('decimal_places', UNIT_PRICE_PLACES)
        super().__init__(*args, **kwargs)

    def get_db_prep_save(self, value, connection):
        if value is not None and not hasattr(value, 'resolve_expression'):
            value = unit_price(self.to_python(value))
        return super().get_db_prep_save(value, connection)

    def deconstruct(self):
        name, _, args, kwargs = super().deconstruct()
        return name, 'django.db.models.DecimalField', args, kwargs


class Item(models.Model):
    """Editable inventory. Imported rows carry the QB POS Inventory SID in source_id."""
    source_id = models.CharField(max_length=100, null=True, blank=True, unique=True, editable=False)
    sku = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200, blank=True)
    upc = models.CharField(max_length=100, blank=True)
    alu = models.CharField(max_length=100, blank=True)
    item_type = models.CharField(max_length=20, default='inventory', choices=[('inventory', 'Inventory'), ('noninventory', 'Non-Inventory')])
    size = models.CharField(max_length=100, blank=True)
    wood = models.CharField(max_length=100, blank=True)
    style = models.CharField(max_length=100, blank=True)
    unit_of_measure = models.CharField(max_length=50, blank=True)
    manufacturer = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=200, blank=True)
    oil_su = models.CharField(max_length=200, blank=True)
    reverse_notavail = models.CharField(max_length=200, blank=True)
    pull = models.CharField(max_length=200, blank=True)
    finish = models.CharField(max_length=200, blank=True)
    tax_category = models.CharField(max_length=100, blank=True)
    quick_pick_group = models.CharField(max_length=100, blank=True)
    reorder_point = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    price_2 = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    price_3 = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    price_4 = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    price_5 = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    price_6 = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    msrp = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    comments = models.TextField(blank=True)
    print_tags = models.BooleanField(default=True)
    unorderable = models.BooleanField(default=False)
    use_serial_numbers = models.BooleanField(default=False)
    earn_commission = models.BooleanField(default=False)
    earn_rewards = models.BooleanField(default=False)
    shipping_weight = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    shipping_height = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    shipping_length = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    shipping_width = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    shipping_weight_unit = models.CharField(max_length=5, blank=True, choices=[('lb', 'lb'), ('kg', 'kg')])
    shipping_dimension_unit = models.CharField(max_length=5, blank=True, choices=[('in', 'inches'), ('cm', 'cm')])
    photo = models.BinaryField(null=True, editable=False)
    description = models.CharField(max_length=1000)
    price = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    on_hand = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, null=True)
    vendor = models.CharField(max_length=200, blank=True)
    primary_vendor = models.ForeignKey('purchasing.Vendor', null=True, blank=True, on_delete=models.PROTECT)
    department = models.ForeignKey('purchasing.Department', null=True, blank=True, on_delete=models.PROTECT)
    order_cost = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    average_cost = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    # QB POS on-order quantity and last receiving date at backup time.: imported_on_order is kept but no longer read
    # (on order is summed from open PO lines dated 2026 or later, purchasing.catalog_quantities).
    imported_on_order = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, editable=False)
    imported_last_received = models.DateField(null=True, blank=True, editable=False)
    version = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.display_name} · {self.sku}'

    @property
    def display_name(self):
        return self.name or self.sku

    @property
    def item_number(self):
        return self.sku

    @property
    def average_cost_missing_label(self):
        """What to show for a blank average cost. A known count with no cost is a new item waiting for its first
        receiving ("Not received yet"); an unknown (NULL) count is an imported item with no verified stock."""
        return 'Not received yet' if self.on_hand is not None else 'Unverified'

    class Meta:
        ordering = ['sku']
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name='item_price_positive'),
            models.CheckConstraint(condition=models.Q(tax_rate__isnull=True) | models.Q(tax_rate__gte=0, tax_rate__lte=100), name='item_tax_range'),
        ]


class TaxLock(NamedTuple):
    """An order payment's sales-tax figures as reported on the day the money was received (see OrderPaymentTaxLock)."""
    tax: Decimal
    taxable: Decimal
    nontaxable: Decimal
    account_taxable: Decimal
    location: str


class OrderPaymentTaxLock(models.Model):
    """the sales tax inside a customer-order payment is fixed on the day the money is received and
    never changes afterwards — a QuickBooks deposit / deposit-refund receipt (`Document`, deposit_order set) and a
    payment or refund recorded in this app (`OrderPayment`) both carry what the tax ledger reported for them:
    tax, the taxable and non-taxable bases (tax + taxable + nontaxable = the money received), the part of the taxable
    base that was charged to Account (0 unless paid by Account), and the report section it landed in. Later edits to
    the order (a chair added, a price, Tax-exempt) change only its total, tax and balance due; the difference is
    booked on the next payment, on that payment's day (reports.tax_ledger.lock_payment). Null = not locked yet: the
    ledger then computes the figures from the order as it stands (the behavior before payment tax locking) until `lock_order_payment_tax`
    stores them.
    """
    locked_tax = MoneyField(null=True, blank=True, editable=False)
    locked_taxable = MoneyField(null=True, blank=True, editable=False)
    locked_nontaxable = MoneyField(null=True, blank=True, editable=False)
    locked_account_taxable = MoneyField(null=True, blank=True, editable=False)
    locked_location = models.CharField(max_length=100, blank=True, editable=False)
    LOCK_FIELDS = ('locked_tax', 'locked_taxable', 'locked_nontaxable', 'locked_account_taxable', 'locked_location')

    class Meta:
        abstract = True

    @property
    def tax_lock(self):
        """The locked figures as a TaxLock, or None when this payment has not been locked."""
        if self.locked_tax is None:
            return None
        return TaxLock(self.locked_tax, self.locked_taxable or Decimal('0.00'), self.locked_nontaxable or Decimal('0.00'),
                       self.locked_account_taxable or Decimal('0.00'), self.locked_location)

    def set_tax_lock(self, lock):
        self.locked_tax, self.locked_taxable, self.locked_nontaxable = lock.tax, lock.taxable, lock.nontaxable
        self.locked_account_taxable, self.locked_location = lock.account_taxable, lock.location


class Document(OrderPaymentTaxLock):
    account_estimate_rate = models.DecimalField(max_digits=6, decimal_places=3, default=default_estimate_rate, editable=False)
    account_estimate_location = models.CharField(max_length=100, default=default_estimate_location, editable=False)
    KINDS = [('sale', 'Sales Receipt'), ('order', 'Sales Order')]
    STATES = [('held', 'Held'), ('open', 'Open'), ('completed', 'Completed'), ('cancelled', 'Cancelled')]
    RECEIPT_TYPES = [(0, 'Sales'), (1, 'Sales reversal'), (2, 'Deposit'), (3, 'Deposit reversal'), (4, 'Payout')]
    # QB backup provenance, or reserved demo- IDs for generated native documents.
    source_id = models.CharField(max_length=100, null=True, blank=True, unique=True, editable=False)
    source_number = models.PositiveIntegerField(null=True, blank=True, db_index=True, editable=False)
    number_value = models.PositiveIntegerField(null=True, blank=True, editable=False)
    receipt_type = models.SmallIntegerField(null=True, blank=True, choices=RECEIPT_TYPES, editable=False)
    cashier = models.CharField(max_length=100, blank=True)
    clerk = models.CharField(max_length=100, blank=True)
    store = models.SmallIntegerField(null=True, blank=True)
    workstation = models.SmallIntegerField(null=True, blank=True, editable=False)  # QB POS workstation number (imported)
    deposit_taken = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    deposit_used = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    was_completed = models.BooleanField(default=False)
    payment_breakdown = models.JSONField(default=list, blank=True)

    @property
    def tender_details(self):
        return [dict(p, label=tender_label(p)) for p in self.payment_breakdown or []]

    @property
    def tender_lines(self):
        """an app receipt's payments as QB POS prints them: one line per tender with the amount handed over, and
        the Change line directly under the cash tender the change came out of (the last cash line on a split). A
        receipt with no cash and no change gets no Change line. Refunds (total below zero) list what was given back
        and never have change; the printed receipt words those itself. Display only: amounts are as stored."""
        entries = self.payment_breakdown or ([{'method': self.payment_method, 'amount': str(self.tendered)}]
                                             if self.payment_method and self.tendered is not None else [])
        rows = []
        for entry in entries:
            try:
                amount = money(Decimal(str(entry.get('amount'))))
            except (ArithmeticError, TypeError, ValueError):
                continue
            rows.append(dict(entry, label=tender_label(entry), amount=amount, change=False))
        if self.total is None or self.total < 0 or self.tendered is None:
            return rows
        change = self.change
        cash = [i for i, row in enumerate(rows) if row.get('method') == 'cash']
        line = dict(label='Change', amount=change, change=True)
        if cash:
            rows.insert(cash[-1] + 1, line)
        elif change:
            rows.append(line)   # older receipts only: the register refuses change without a cash tender
        return rows

    customer_po = models.CharField(max_length=100, blank=True)
    source_order = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='fulfillments')
    # QB POS deposit / deposit-refund receipt (types 2, 3): the customer order it was paid toward (receipt SORefSID).
    # Sales tax on customer orders is reported on pickup receipts.
    deposit_order = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='deposit_receipts', editable=False)
    tax_exempt = models.BooleanField(default=False)
    # QB POS tax location of the receipt (a Tax Settings location name) and any receipt-level discount taken off the
    # subtotal before tax. Blank / zero on native documents.
    tax_location = models.CharField(max_length=100, blank=True, editable=False)
    discount = MoneyField(max_digits=12, decimal_places=2, default=0, editable=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    discount_reason = models.CharField(max_length=100, blank=True)
    discount_note = models.CharField(max_length=200, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=10, choices=KINDS)
    status = models.CharField(max_length=12, choices=STATES)
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, null=True)
    customer_name = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    shipping_address = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    payment_method = models.CharField(max_length=10, blank=True)
    tendered = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    # Null only when the backup's Total annotation could not be decoded (2 deposit receipts); never for native documents.
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True)
    # a receipt put on hold in the app for a real customer reserves its items (holds_stock) until it is finished,
    # cancelled, deleted or its hold runs out (hold_until, end of the store day; null = no expiry). On Hand stays
    # physical; Available = On Hand - open order qty - held (sales.holds). hold_expired_at is stamped lazily when
    # hold_until has passed: the receipt stays on the Held Receipts list marked Expired and reserves nothing until
    # the hold is extended. Set signs (no customer), QUOTE receipts and imported QB held receipts never reserve.
    hold_until = models.DateTimeField(null=True, blank=True)
    holds_stock = models.BooleanField(default=False)
    hold_expired_at = models.DateTimeField(null=True, blank=True)
    # Hold for Pickup. A paid, finished sale whose items are still in the store because the customer
    # comes back for them (sales.pickup). The sale itself is unchanged (money, tax, stock, reports); these fields only
    # say the goods are waiting and, once collected, when and by whom it was handed over. Never a held receipt (above),
    # never on returns, order pickups or cancelled receipts; imported QB receipts start not held.
    hold_for_pickup = models.BooleanField(default=False)
    est_pickup_date = models.DateField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    picked_up_by = models.CharField(max_length=100, blank=True)  # the active cashier's name, like `cashier`
    # a sales order's "Hold for Pickup" box: the customer will pick the goods up here (ticked by
    # default; staff untick it when a third party delivers). Only ticked open orders are listed on Awaiting Pickup.
    # Sales orders only (receipts use `hold_for_pickup` above); no money, tax or stock effect.
    order_pickup = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.CheckConstraint(
            condition=models.Q(kind='order', status__in=['open', 'completed', 'cancelled']) | models.Q(kind='sale', status__in=['held', 'completed', 'cancelled']),
            name='document_kind_state'),
            models.UniqueConstraint(fields=['kind', 'number_value'], name='document_number_per_kind')]

    def save(self, *args, **kwargs):
        from django.db import router, transaction
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        # The counter lock spans the document write (and the enclosing service transaction).
        with transaction.atomic(using=using):
            if (not imported_source(self.source_id) and self.source_number is None and self.number_value is None
                    and (self.kind == 'order' or self.status == 'completed')):
                counter, _ = DocumentNumberCounter.objects.using(using).select_for_update().get_or_create(
                    kind=self.kind, defaults={'next_number': 10000 if self.kind == 'order' else 200000})
                candidate = counter.next_number
                docs = Document.objects.using(using).filter(kind=self.kind)
                while docs.filter(models.Q(number_value=candidate) | models.Q(source_number=candidate)).exists():
                    candidate += 1
                self.number_value = candidate
                counter.next_number = candidate + 1
                counter.save(using=using, update_fields=['next_number'])
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = set(kwargs['update_fields']) | {'number_value'}
            super().save(*args, **kwargs)

    @property
    def from_quickbooks(self):
        """Loaded from the QB POS backup (provenance), whatever its state now."""
        return imported_source(self.source_id)

    @property
    def imported(self):
        """Read-only QB history: receipts (sales history) and closed or cancelled sales orders from QuickBooks.
        Two kinds of QB document are workable instead: a held receipt (unheld and finished like any held receipt; it
        becomes an app receipt when saved, see services.save_document) and an OPEN sales order ("make imported open orders work like app-made ones" — deposits, pickups, cancel and edits on top of what
        QuickBooks recorded; it keeps its source id and number)."""
        return imported_source(self.source_id) and self.status != 'held' and not (self.kind == 'order' and self.status == 'open')

    @property
    def store_day(self):
        """The store date (America/New_York) the receipt was completed on; None when never completed."""
        return timezone.localdate(self.completed_at) if self.completed_at else None

    @property
    def locked(self):
        """A completed (or cancelled) app receipt is read-only from the day after its store day (as QB:
        a saved receipt is never edited; corrections are returns dated today). Imported QB receipts are read-only
        anyway (`imported`). Only an Admin Override (services.save_document) changes a locked receipt."""
        return (self.kind == 'sale' and not self.imported and self.status in ('completed', 'cancelled')
                and self.completed_at is not None and self.store_day < store_today())

    @property
    def number(self):
        if self.status == 'held':
            return ''
        if self.source_number is not None:
            return str(self.source_number)
        return str(self.number_value) if self.number_value is not None else ''

    @property
    def number_or_label(self):
        return self.number or ('Held' if self.status == 'held' else 'Cancelled' if self.status == 'cancelled' else 'Unnumbered')

    @property
    def numbered_label(self):
        label = 'Sales Order' if self.kind == 'order' else 'Receipt'
        return f'{label} {self.number}' if self.number else self.number_or_label

    @property
    def type_label(self):
        """Receipt type from the backup for non-sales receipts (Deposit, Payout, reversals); blank otherwise."""
        return self.get_receipt_type_display() if self.receipt_type not in (None, 0) else ''

    @property
    def payment_label(self):
        methods = dict(PAYMENT_METHODS)
        if self.imported:
            labels = [t.label for t in self.tenders.all() if not t.use_in_total]
            return ', '.join(dict.fromkeys(labels)) if labels else 'None'
        if self.source_order_id:
            methods = list(dict.fromkeys(p.label for p in self.source_order.payments.all() if p.is_tender))
            return 'Order payments: ' + ', '.join(methods) if methods else 'Order payments'
        if self.payment_breakdown:
            return ', '.join(dict.fromkeys(methods.get(p['method'], p['method']) for p in self.payment_breakdown))
        return methods.get(self.payment_method, 'None')

    @property
    def paid(self):
        """Native payments plus the deposit QuickBooks had already taken on an imported order."""
        return sum((p.amount for p in self.payments.all()), Decimal('0.00')) + (self.deposit_taken or Decimal('0.00'))

    @property
    def qb_deposit(self):
        """The deposit QuickBooks POS recorded on this order, as one row for the order page and printouts:
        dict(amount, used, date, count, label) or None when QuickBooks took none. `amount` is QB's own figure
        (`deposit_taken`, what `paid` counts), `date` the latest linked QB deposit receipt's store day (the order's
        own date when none is linked), `count` how many QB deposit/refund receipts are linked."""
        if not self.from_quickbooks or not self.deposit_taken:
            return None
        receipts = [r for r in self.deposit_receipts.all() if r.status == 'completed' and r.receipt_type in (2, 3)]
        latest = max((r.completed_at for r in receipts if r.completed_at), default=None) or self.created_at
        return dict(amount=self.deposit_taken, used=self.deposit_used or Decimal('0.00'), date=timezone.localdate(latest),
                    count=len(receipts), label='Deposit taken in QuickBooks POS')

    @property
    def qb_other_charge(self):
        """review, finding 2: a charge QuickBooks put in an imported order's total beyond its lines and tax
        (shipping or another charge; 4 open orders in the 10/18 data, 2920, 3926, 4219 and 4444, $12 to $120). The
        app has no field for it, so such an order refuses money edits and pickups (services) until it can be kept.
        0.00 for app orders and for a QB order once it has been recomputed."""
        if not self.from_quickbooks or self.kind != 'order' or self.total is None:
            return Decimal('0.00')
        return money(self.total - ((self.subtotal or Decimal('0')) - (self.discount or Decimal('0')) + (self.tax or Decimal('0'))))

    @property
    def qb_deposit_mismatch(self):
        """review, finding 4: (QuickBooks' deposit_taken, sum of the QB deposit receipts linked to the order) when
        they disagree, else None. `paid` counts QB's figure while the tax ledger replays the receipts, so a cancel,
        refund or pickup on such an order would leave the two out of step (1393 and 2890 in the 10/18 data); services
        refuse those until the office reconciles the order against QuickBooks."""
        if not self.from_quickbooks or self.kind != 'order':
            return None
        taken = self.deposit_taken or Decimal('0.00')
        linked = sum((r.total or Decimal('0') for r in self.deposit_receipts.all()
                      if r.status == 'completed' and r.receipt_type in (2, 3)), Decimal('0.00'))
        return (money(taken), money(linked)) if money(taken) != money(linked) else None

    @property
    def balance_due(self):
        if self.kind == 'order' and self.status == 'cancelled':
            return Decimal('0.00')   # a cancelled order owes nothing (its deposit is refunded at Cancel)
        return money((self.total or Decimal('0')) - self.paid)

    @property
    def applied_payments(self):
        """Imported pickups consumed deposit_used in QuickBooks; native pickups consume their own totals."""
        native = self.fulfillments.filter(native_source(), status='completed').values_list('total', flat=True)
        return sum(native, Decimal('0')) + (self.deposit_used or Decimal('0'))

    @property
    def unused_payments(self):
        return money(self.paid - self.applied_payments)

    @property
    def unfilled(self):
        return sum((max(line.quantity - line.fulfilled, Decimal('0')) for line in self.lines.all()), Decimal('0'))

    @property
    def change(self):
        return money(self.tendered - self.total) if self.tendered is not None and self.total is not None else Decimal('0.00')

    # --- Hold for Pickup (see sales.pickup) ---
    @property
    def awaiting_pickup(self):
        """A completed sale held for pickup that hasn't been collected yet."""
        return self.kind == 'sale' and self.status == 'completed' and self.hold_for_pickup and self.picked_up_at is None

    @property
    def picked_up(self):
        return self.kind == 'sale' and self.status == 'completed' and self.hold_for_pickup and self.picked_up_at is not None

    @property
    def pickup_status(self):
        """'Awaiting Pickup – est. 10/30', 'Awaiting Pickup', 'Completed, picked up 10/30 by AC' or '' (not held)."""
        from .pickup import status_text
        return status_text(self)

    # --- hold state (see sales.holds) ---
    @property
    def hold_date(self):
        """Store date the hold runs to (None = no expiry or not held)."""
        return timezone.localdate(self.hold_until) if self.hold_until else None

    @property
    def hold_expired(self):
        return self.hold_expired_at is not None or bool(self.hold_until and self.hold_until < timezone.now())

    @property
    def hold_reserving(self):
        """True while this held receipt actually keeps its items out of Available."""
        return self.status == 'held' and self.holds_stock and not self.hold_expired

    @property
    def hold_label(self):
        """Held Receipts list / details text: 'Expired 10/26/2026', 'Until 10/26/2026', 'No expiry' or 'No stock set aside' (set
        signs, quotes and QB held receipts, which reserve nothing)."""
        if self.status != 'held' or not self.holds_stock:
            return 'No stock set aside'
        if self.hold_until is None:
            return 'No expiry'
        when = self.hold_date.strftime('%-m/%-d/%Y')
        return f'Expired {when}' if self.hold_expired else f'Until {when}'


class ReceiptOverride(models.Model):
    """Permanent record of an Admin Override on a locked (past-day) receipt: who, when, what changed and why.
    Only a signed-in admin can override; `performed_by` is that admin's username (older rows may read
    'Admin Mode (no login)' from before admin logins) and `client_address` the workstation's IP address."""
    ACTIONS = [('save', 'Saved changes'), ('cancel', 'Cancelled the receipt')]
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name='overrides')
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    performed_by = models.CharField(max_length=150, blank=True)
    client_address = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=10, choices=ACTIONS)
    reason = models.TextField()
    summary = models.TextField(blank=True)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.CheckConstraint(condition=~models.Q(reason=''), name='receipt_override_reason_required')]


class DocumentNumberCounter(models.Model):
    """One locked sequence per sales-document kind; never a database row-id sequence."""
    kind = models.CharField(max_length=10, choices=Document.KINDS, primary_key=True)
    next_number = models.PositiveIntegerField()


class Line(models.Model):
    source_order_line = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='deliveries')
    # Accept Return (B1): a return line (negative quantity) made in the app points at the receipt line it returns.
    # RESTRICT: the original line can't be removed while a return points at it (a full wipe deletes both together).
    returned_from_line = models.ForeignKey('self', null=True, blank=True, on_delete=models.RESTRICT, related_name='returns')
    # QuickBooks' own return link on imported lines (receipt item DocRefSID): the original *receipt* a QB return line
    # was taken against (QB links the receipt, not the line). Used to count QB-era returns in the Accept Return cap.
    returned_from_receipt = models.ForeignKey('Document', null=True, blank=True, on_delete=models.SET_NULL,
                                              related_name='qb_return_lines', editable=False)
    # "Damaged — don't restock" on a return line: the returned quantity does NOT go back into sellable
    # stock (no stock movement). Kept on the line so inventory reports can list damaged returns.
    damaged = models.BooleanField(default=False)
    # Why it came back (RETURN_REASONS value) and the cashier's note; blank on sold lines.
    return_reason = models.CharField(max_length=20, blank=True, choices=RETURN_REASONS)
    return_note = models.CharField(max_length=200, blank=True)
    # The optional restocking fee line on a return receipt (computed by the server from the return lines).
    restocking_fee = models.BooleanField(default=False)
    source_id = models.CharField(max_length=120, null=True, blank=True, editable=False)
    document = models.ForeignKey(Document, related_name='lines', on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True)
    sku = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=200, blank=True)
    description = models.CharField(max_length=1000)
    vendor = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    # Unit price as QB keeps it: whole cents when typed, up to 4 decimals after a % line discount. Shown to 2.
    price = PriceField(max_digits=16, decimal_places=UNIT_PRICE_PLACES)
    # QB POS regular price at the time of sale and the discount reason QuickBooks recorded (e.g. "Sale", "Defect").
    # Null/blank on native lines. A line is discounted when price < original_price.
    original_price = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True, editable=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    discount_reason = models.CharField(max_length=100, blank=True, editable=False)
    # % off the regular price on this line (QB DiscountPercent): price = original_price x (100 - %) / 100, exact to
    # 4 decimals. 0 when the price was typed or the line is not discounted.
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    certificate_number = models.CharField(max_length=100, blank=True)
    discount_note = models.CharField(max_length=200, blank=True)
    # This line's share of the receipt-level % discount (QB ReceiptItem.RealPrice), allocated by
    # allocate_receipt_discount; 0 when the receipt has none. `price` and `amount` stay gross, as on QB's receipt.
    receipt_discount = MoneyField(max_digits=16, decimal_places=2, default=0)

    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, null=True)
    unit_cost = MoneyField(max_digits=16, decimal_places=2, null=True)
    # QB's recorded line total, kept only on imported lines whose QB price had fractions of a cent:
    # the price shows in whole cents and the line total still matches QB's receipt. Cleared when the app saves the line.
    recorded_amount = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True, editable=False)

    class Meta:
        ordering = ['position']
        # Native lines are validated; imported lines are stored exactly as QuickBooks recorded them
        # (returns are negative quantities, a handful of lines carry zero quantity or a negative price).
        constraints = [
            # App lines: positive, or negative only as a return linked to the original receipt line (B1).
            models.CheckConstraint(condition=models.Q(quantity__gt=0) | models.Q(source_id__isnull=False)
                                   | models.Q(quantity__lt=0, returned_from_line__isnull=False), name='line_quantity_positive_or_return'),
            models.CheckConstraint(condition=models.Q(price__gte=0) | models.Q(source_id__isnull=False), name='line_price_positive'),
        ]

    @property
    def fulfilled(self):
        return sum((line.quantity for line in self.deliveries.filter(document__status='completed')), Decimal('0'))

    @property
    def return_reason_text(self):
        """'Changed mind - box was too big' style text for receipts and lists ('' when neither is set)."""
        return ' - '.join(filter(None, [self.get_return_reason_display() if self.return_reason else '', self.return_note]))

    @property
    def is_return(self):
        return self.quantity is not None and self.quantity < 0

    @property
    def discounted(self):
        return self.original_price is not None and self.price < self.original_price

    @property
    def dollars_off(self):
        """the dollar discount off each unit (QB's Unit Discount) - regular price less the price, for a line with no
        % off whose price is below regular in whole cents (a $ line discount, or a price typed below regular, which QB
        treats the same). None otherwise."""
        if self.discount_percent or not self.discounted:
            return None
        off = self.original_price - self.price
        return off if off == off.quantize(Decimal('0.01')) and self.price == self.price.quantize(Decimal('0.01')) else None

    @property
    def amount(self):
        """Gross extension (qty x price, half-up), before any receipt-level discount — QB's Ext. column."""
        return line_amount(self.quantity, self.price, self.recorded_amount)

    @property
    def net_amount(self):
        """What the line actually sells for: the extension less this line's share of the receipt discount."""
        return self.amount - (self.receipt_discount or Decimal('0'))

    @property
    def tax_basis(self):
        """What sales tax is figured on: the UNROUNDED extension (qty x exact price, e.g. 1 x 109.975) less the
        receipt-discount share. QB rounds tax once per rate on these exact amounts, not on the cent-rounded line
        totals: 129.95 + 109.975 = 239.925 x 7 % = 16.79 (QB receipt 106279), not 239.93 x 7 % = 16.80. Verified on
        every discounted receipt since 9/2021 (the import parity checks). Whole-cent prices make
        this identical to net_amount."""
        exact = self.recorded_amount if self.recorded_amount is not None else Decimal(self.quantity) * Decimal(self.price)
        return exact - (self.receipt_discount or Decimal('0'))


class StockMovement(models.Model):
    """Current stock effect of a receipt, reconciled when the receipt is edited."""
    document = models.ForeignKey(Document, on_delete=models.PROTECT)
    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['document', 'item'], name='one_stock_movement_per_receipt_item')]


class ImportLog(models.Model):
    """A reviewed export batch and its reversible writes. Payloads stay on the server."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    status = models.CharField(max_length=12, default='preview', choices=[
        ('preview', 'Preview'), ('committed', 'Imported'), ('undone', 'Undone')])
    files = models.JSONField(default=list)
    options = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    payload = models.JSONField(default=dict, editable=False)
    changes = models.JSONField(default=list, editable=False)
    committed_at = models.DateTimeField(null=True)
    undone_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-created_at']


PAYMENT_METHODS = [('cash', 'Cash'), ('check', 'Check'), ('credit', 'Credit card'), ('debit', 'Debit card'), ('gift', 'Gift certificate/card'), ('account', 'Account'), ('other', 'Other')]


CARD_TYPES = ('Visa', 'MasterCard', 'American Express', 'Discover')


def tender_label(entry):
    label = entry.get('tender_name') or dict(PAYMENT_METHODS).get(entry.get('method'), entry.get('method', ''))
    return label + (' #' + entry['reference'] if entry.get('reference') else '')


class DiscountReason(models.Model):
    name = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']


def discount_text(reason, note=''):
    return ' - '.join(filter(None, [reason.replace('Price Descrepancy', 'Price Discrepancy'), note]))


class GiftCertificate(models.Model):
    number = models.CharField(max_length=100, unique=True)
    issued_on = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='issued_certificates')
    face_value = MoneyField(max_digits=12, decimal_places=2)
    balance = MoneyField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['number']


class GiftRedemption(models.Model):
    certificate = models.ForeignKey(GiftCertificate, on_delete=models.PROTECT, related_name='redemptions')
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='gift_redemptions')
    amount = MoneyField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['certificate', 'document'], name='one_gift_redemption_per_receipt')]


class Tender(models.Model):
    """One row per QB POS ReceiptTender on an imported receipt.

    use_in_total=False rows count toward the receipt total (their sum equals Document.total); use_in_total=True rows
    are the memo/offset lines QuickBooks records for account credits. Change is a negative cash row.
    """
    document = models.ForeignKey(Document, related_name='tenders', on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    # Null only when the backup's TenderAmount annotation could not be decoded (4 rows).
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    tender_name = models.CharField(max_length=100, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    use_in_total = models.BooleanField(default=False)
    source_id = models.CharField(max_length=120, null=True, blank=True, editable=False)

    class Meta:
        ordering = ['position', 'pk']
        constraints = [models.UniqueConstraint(fields=['document', 'position'], name='one_tender_per_position')]

    @property
    def label(self):
        return self.tender_name or self.get_method_display()


class OrderPayment(OrderPaymentTaxLock):
    """A payment or refund (negative) recorded in this app on a customer order. Its tax figures are locked when it
    is recorded (OrderPaymentTaxLock).

    a payment from an EARLIER store day is immutable — its amount, method and date can't be
    changed and it can't be deleted (services.save_order_payment); only its reference stays editable. A payment is
    always dated the day it is recorded. `adjustment` marks a $0 row the app itself books when an order settles
    (`reports.tax_ledger.settle_order_tax`): it carries only a tax lock ("Sales tax adjustment"), is never a tender,
    and staff can't edit or delete it."""
    order = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='payments')
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    reference = models.CharField(max_length=200, blank=True)
    # (matching QB): the card type on a credit card payment (Visa, MasterCard,...; CARD_TYPES), like a sale's
    # tender_name. Blank for other methods. Not money: changing it alone never touches the tax lock.
    card_type = models.CharField(max_length=100, blank=True)
    # (matching QB, whose deposit receipts record a cashier): who took the payment or refund — the active PIN
    # cashier or the signed-in admin when it was recorded (services.save_order_payment, the cancel refund). A same-day
    # correction keeps it (OrderPaymentChange logs who corrected). Blank for payments recorded before, with no
    # active cashier, and on tax-adjustment rows. Same length as Document.cashier.
    cashier = models.CharField(max_length=100, blank=True)
    # (matching QB, whose deposit receipts record the cash handed over and the change): on a Cash payment, the
    # amount the customer handed over (Amount received), for the order screen and printout only. Never money: the
    # payment, its tax lock and Daily Totals count `amount`, not this. Null = not entered (and for every other method).
    tendered = MoneyField(max_digits=12, decimal_places=2, null=True, blank=True)
    # (matching QB, whose check tenders carry a check number: every QB check tender since 2019 but one has it):
    # the check number on a Check payment or refund ("1470"; shown as "Check #1470", like a sale's check tender).
    # Required on a new check row (services.save_order_payment). Not money: changing it alone never touches the tax
    # lock, and an earlier-day payment may change it like the reference. Blank for other methods and for payments
    # recorded before (their check number, if any, stays in the reference as typed).
    check_number = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    adjustment = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ['created_at', 'pk']

    @property
    def change_given(self):
        """Change handed back on a cash payment with an Amount received, else None."""
        if self.method != 'cash' or self.tendered is None or self.amount is None or self.amount <= 0:
            return None
        return self.tendered - self.amount

    @property
    def label(self):
        """How it was paid, as printed: the card type for a card payment that has one, "Check #1470" for a check with
        a number (as a sale's check tender prints), else the method."""
        if self.method == 'check' and self.check_number:
            return f'Check #{self.check_number}'
        return (self.card_type if self.method == 'credit' else '') or self.get_method_display()

    @property
    def store_day(self):
        """The store date (America/New_York) the payment was recorded on."""
        return timezone.localdate(self.created_at)

    @property
    def earlier_day(self):
        """Recorded on an earlier store day than today: money fields and date are fixed, no delete."""
        return self.store_day < store_today()

    @property
    def is_tender(self):
        """Money that moved (a payment or refund), as opposed to a sales tax adjustment."""
        return not self.adjustment


class OrderPaymentChange(models.Model):
    """Append-only record of a correction or delete of an order payment (the order-payment part of the
    Activity Log): who (signed-in login, active cashier, workstation address), what changed and the order. Written by
    services.save_order_payment inside the same transaction; never edited."""
    ACTIONS = [('correct', 'Corrected'), ('delete', 'Deleted')]
    order = models.ForeignKey(Document, on_delete=models.PROTECT, related_name='payment_changes')
    payment = models.ForeignKey(OrderPayment, on_delete=models.SET_NULL, null=True, blank=True, related_name='changes')
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    action = models.CharField(max_length=10, choices=ACTIONS)
    performed_by = models.CharField(max_length=150, blank=True)
    cashier = models.CharField(max_length=150, blank=True)
    client_address = models.CharField(max_length=64, blank=True)
    summary = models.TextField(blank=True)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)

    class Meta:
        ordering = ['created_at', 'pk']

    @property
    def who(self):
        parts = [p for p in (self.performed_by, self.cashier) if p]
        return ' · '.join(dict.fromkeys(parts)) if parts else 'unknown'


class InventoryAdjustment(models.Model):
    item = models.ForeignKey(Item, related_name='adjustments', on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    old_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    new_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True)
    old_cost = MoneyField(max_digits=16, decimal_places=2, null=True)
    new_cost = MoneyField(max_digits=16, decimal_places=2, null=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    @property
    def quantity_change(self):
        return self.new_quantity - self.old_quantity if self.old_quantity is not None and self.new_quantity is not None else None


class Printer(models.Model):
    """Store printers the server prints to directly. Configuration, not test business data: keep at cutover."""
    KINDS = [('receipt', 'Receipt (Star, 72mm)'), ('letter', 'Letter (sales orders)'),
             ('label', 'Label (Zebra 4×3 tags)')]
    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField('Type', max_length=10, choices=KINDS, default='receipt')
    model = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=100, blank=True)
    host = models.CharField('IP address or hostname', max_length=100)
    port = models.PositiveIntegerField(default=9100)
    mac_address = models.CharField('MAC address', max_length=17, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    # Letter printers only. Color: pages that contain color (the green set-tag message) go out as a color raster;
    # black-and-white pages stay monochrome. Paper tray: IPP media-source keyword; blank = the printer's own choice.
    MEDIA_SOURCES = [('', 'Auto (printer chooses)'), ('tray-1', 'Tray 1'), ('tray-2', 'Tray 2'), ('tray-3', 'Tray 3'),
                     ('tray-4', 'Tray 4'), ('manual', 'Manual feed (MP tray)')]
    color = models.BooleanField('Color printer', default=False,
                                help_text='Letter printers: print color pages (such as the green set-tag message) in color.')
    media_source = models.CharField('Paper tray', max_length=20, blank=True, choices=MEDIA_SOURCES,
                                    help_text='Letter printers: which tray to print from.')
    # how many copies the sales order Copies box starts at when this printer is picked (staff can still change it).
    order_copies = models.PositiveSmallIntegerField('Sales order copies', default=3,
                                                    validators=[MinValueValidator(1), MaxValueValidator(5)],
                                                    help_text='Letter printers: copies a sales order starts at on this printer (1 to 5).')
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [models.CheckConstraint(condition=models.Q(port__gte=1, port__lte=65535), name='printer_port_range')]

    def __str__(self):
        return self.name

    @property
    def can_print(self):
        return self.active and self.kind == 'receipt'


class ItemNumberCounter(models.Model):
    last_number = models.PositiveBigIntegerField(default=0)


class ItemTrash(models.Model):
    """One row per item in the Items Trash: who moved it, when, and the stock zeroed so Restore can put it back.

    Keyed by item number (sku) so it survives a rebuild from a QB backup, which reloads every item as active:
    `sales.trash.reapply` re-trashes these skus after the load. Deleted on Restore. Not wiped by the importer.
    """
    item = models.OneToOneField(Item, null=True, blank=True, on_delete=models.SET_NULL, related_name='trash_entry', editable=False)
    sku = models.CharField(max_length=100, unique=True)
    item_name = models.CharField(max_length=200, blank=True)
    trashed_at = models.DateTimeField(default=timezone.now)
    trashed_by = models.CharField(max_length=100, blank=True)
    # On-hand quantity zeroed when the item went to the Trash (None when the stock was unverified / already 0).
    quantity_zeroed = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    # The inventory value that went with it (quantity × average cost at that moment), for the record. While the item is
    # in the Trash the parked quantity still counts in its average cost (see purchasing.services / sales.trash.parked),
    # so Restore brings back the same quantity AND average cost the item would have had if it had never left.
    value_zeroed = MoneyField(null=True, blank=True)

    class Meta:
        ordering = ['-trashed_at', '-pk']

    def __str__(self):
        return f'{self.item_name or self.sku} · trashed {self.trashed_at:%m/%d/%Y}'


class DiscountApproval(models.Model):
    """Hashed, one-use grant. Consumption shares the document's transaction and row lock."""
    token_hash = models.CharField(max_length=64, unique=True)
    session_hash = models.CharField(max_length=64)
    document_token = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
