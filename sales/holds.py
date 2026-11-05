# SPDX-License-Identifier: AGPL-3.0-or-later
"""Held receipts that hold their stock.

A receipt put on hold in the app for a real customer reserves its items until it is finished, cancelled, deleted or
its hold runs out. Reservations are not stock movements: `Item.on_hand` stays the physical count and
    Available = On Hand - open customer order qty (purchasing.reorder.committed_by_item) - held (held_by_item here).

Rules:
  1. Only held receipts with a real customer hold stock. Set signs (no customer) and QUOTE receipts do not.
  2. New holds only: held receipts imported from QuickBooks reserve nothing (sales.rebuild untouched).
  3. On Hand stays physical; Held and Available are shown beside it.
  4. When the hold runs out the stock is released and the receipt is marked Expired on the Held Receipts list; it
     stays there and can be finished, extended or deleted. Expiry is stamped lazily (expire_overdue_holds) whenever
     stock figures or the held list are read - no scheduled job.
  5. Hold time presets 1 day / 3 days / 1 week / 2 weeks / 30 days / custom date / no expiry, default 1 week.
"""
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Document, Line

# (code, label, days); custom and none are handled separately.
HOLD_PRESETS = [('1d', '1 day', 1), ('3d', '3 days', 3), ('1w', '1 week', 7), ('2w', '2 weeks', 14), ('30d', '30 days', 30)]
HOLD_CHOICES = [(code, label) for code, label, _ in HOLD_PRESETS] + [('custom', 'Custom date'), ('none', 'No expiry')]
DEFAULT_PRESET = '1w'
PRESET_DAYS = {code: days for code, _, days in HOLD_PRESETS}
QUOTE = re.compile(r'\bQUOTE\b', re.IGNORECASE)
ZERO = Decimal('0')

# Lines on held receipts that currently keep stock out of Available.
RESERVING = Q(document__kind='sale', document__status='held', document__holds_stock=True, document__hold_expired_at__isnull=True)


class HoldError(ValueError):
    """A hold choice the register cannot use; the message is shown to the cashier as typed."""


def reserves_stock(customer):
    """Decision 1: a real customer holds stock; no customer (set signs) or a QUOTE customer does not."""
    if customer is None:
        return False
    names = ' '.join(filter(None, [customer.display_name, customer.first_name, customer.last_name, customer.company]))
    return not QUOTE.search(names)


def end_of_day(day):
    """The hold runs to the end of the chosen store day (America/New_York)."""
    return timezone.make_aware(datetime.combine(day, time(23, 59, 59)))


def resolve_hold_until(value, current=None, keep_allowed=False):
    """Turn the register's hold choice into a datetime (None = no expiry).

    `value`: a preset code ('1d', '3d', '1w', '2w', '30d'), 'none', an ISO date for a custom date, or 'keep' to leave
    an existing hold alone (only when `keep_allowed`, i.e. the receipt is already held). Missing = default 1 week for
    a new hold, keep for a re-saved held receipt.
    """
    value = (value or '').strip() if isinstance(value, str) else ('' if value is None else str(value))
    if not value:
        value = 'keep' if keep_allowed else DEFAULT_PRESET
    if value == 'keep':
        if not keep_allowed:
            raise HoldError('Choose how long to hold the items.')
        return current
    if value == 'none':
        return None
    today = timezone.localdate()
    if value in PRESET_DAYS:
        return end_of_day(today + timedelta(days=PRESET_DAYS[value]))
    try:
        chosen = date.fromisoformat(value)
    except ValueError:
        raise HoldError('Choose a hold time or enter a valid hold-until date.')
    if chosen < today:
        raise HoldError('The hold-until date has already passed. Choose today or a later date.')
    if chosen > today + timedelta(days=366 * 5):
        raise HoldError('Hold-until dates can be at most five years out. Choose No expiry for an open-ended hold.')
    return end_of_day(chosen)


def apply_hold(document, customer, value, was_held, now=None):
    """Set the hold fields on a receipt being put (or kept) on hold. `was_held`: the receipt was already held before
    this save. 'keep' (also the default when nothing is sent) is allowed only when that earlier hold actually held
    stock (`holds_stock`); an imported QuickBooks hold, a QUOTE hold or a set sign never had a stock hold, so putting it
    on hold for a real customer starts a fresh hold with the 1-week default instead of an open-ended one. An app hold
    saved with No expiry keeps it on 'keep'. A new hold_until clears an earlier expiry so an extended hold reserves the
    items again; 'keep' leaves an expired hold expired."""
    now = now or timezone.now()
    had_stock_hold = was_held and document.holds_stock  # read before holds_stock is reset below
    document.holds_stock = reserves_stock(customer)
    document.hold_until = resolve_hold_until(value, document.hold_until if had_stock_hold else None,
                                             keep_allowed=had_stock_hold)
    if document.hold_until is None or document.hold_until >= now:
        document.hold_expired_at = None
    elif document.hold_expired_at is None:
        document.hold_expired_at = now


def clear_hold(document):
    """A receipt leaving the held state (completed, cancelled) keeps nothing reserved."""
    document.holds_stock = False
    document.hold_until = None
    document.hold_expired_at = None


def expire_overdue_holds(now=None, receipts=None):
    """Decision 4: stamp hold_expired_at on reserving holds whose time has run out; returns how many expired now."""
    now = now or timezone.now()
    receipts = Document.objects.all() if receipts is None else receipts
    return receipts.filter(kind='sale', status='held', holds_stock=True, hold_expired_at__isnull=True,
                                   hold_until__lt=now).update(hold_expired_at=now)


def reserving_lines():
    """The one rule for which lines are held: positive-quantity inventory lines on a reserving held receipt. Every
    Held/Available figure (Item Detail, Item List, picker, register) is built from this queryset."""
    return Line.objects.filter(RESERVING, quantity__gt=0, item__item_type='inventory')


def held_by_item(item_ids):
    """Quantity reserved on held receipts, per item (decision 3). Expires overdue holds first so the figure is
    current."""
    expire_overdue_holds()
    rows = reserving_lines().filter(item_id__in=item_ids).values('item_id').annotate(total=Sum('quantity'))
    return {row['item_id']: row['total'] for row in rows if row['total']}


def annotate_held(qs):
    """Item queryset annotation `held` for list screens (same rule as held_by_item; expires overdue holds first)."""
    expire_overdue_holds()
    held = (reserving_lines().filter(item_id=OuterRef('pk')).order_by()
            .values('item_id').annotate(n=Sum('quantity')).values('n'))
    return qs.annotate(held=Coalesce(Subquery(held), Value(ZERO), output_field=DecimalField()))


def available(on_hand, committed=ZERO, held=ZERO):
    """Available = On Hand - open order qty - held; None when On Hand is unverified."""
    if on_hand is None:
        return None
    return on_hand - (committed or ZERO) - (held or ZERO)


def available_by_item(items):
    """{item pk: Available} for the register and pickers: On Hand - open customer order qty - held (one query each),
    the same figure everywhere it is labelled Available."""
    from purchasing.reorder import committed_by_item
    ids = [item.pk for item in items]
    committed = committed_by_item(ids)
    held = held_by_item(ids)
    return {item.pk: available(item.on_hand, committed.get(item.pk), held.get(item.pk)) for item in items}
