# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hold for Pickup on a paid sales receipt.

The customer paid in full for in-stock items but will come back for them (the car was full). The sale is finished
exactly like any other: paid, tax on the payment day, stock deducted, in that day's Daily Totals and Sales Tax, two
printed copies, returns as usual. These fields only record that the goods are still in the store:

- `hold_for_pickup` + optional `est_pickup_date`, set at checkout (the "Hold for Pickup" box) or later on the finished
  receipt (the "Hold for Pickup" button);
- `picked_up_at` / `picked_up_by`, stamped by "Mark Picked Up" with the active cashier (as pickups record theirs).

Nothing here touches money, tax or stock, so the actions on a finished receipt work on any later day without the
store-day receipt lock or an Admin Override. It is never a held receipt: those are unpaid and never listed here,
and Put on Hold doesn't apply. Cancelling a receipt clears the hold for pickup (services.save_document).
"""
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from . import accounts
from .models import Document, store_today


class PickupError(Exception):
    pass


def short_date(value):
    return f'{value.month}/{value.day}'


def status_text(doc):
    """Status wording for the receipt screen, Sales History and the confirm text ('' when not held for pickup)."""
    if getattr(doc, 'kind', None) != 'sale' or doc.status != 'completed' or not doc.hold_for_pickup:
        return ''
    if doc.picked_up_at is None:
        return 'Awaiting Pickup' + (f' – est. {short_date(doc.est_pickup_date)}' if doc.est_pickup_date else '')
    when = short_date(timezone.localdate(doc.picked_up_at))
    return f'Completed, picked up {when}' + (f' by {doc.picked_up_by}' if doc.picked_up_by else '')


def history_text(doc):
    """Sales History's status column: 'Awaiting Pickup · est. 10/30' (the list's separator), or the picked-up text."""
    if doc.awaiting_pickup:
        return 'Awaiting Pickup' + (f' · est. {short_date(doc.est_pickup_date)}' if doc.est_pickup_date else '')
    return status_text(doc)


def printed_line(doc):
    """The line on the printed receipt (both copies) while the goods wait; none once picked up or not held. A plain
    hyphen: the receipt printer's font has no en dash (it printed as a box), as on 'TESTING - NOT A VALID RECEIPT'."""
    if not getattr(doc, 'awaiting_pickup', False):
        return ''
    return 'AWAITING PICKUP' + (f' - Est. {short_date(doc.est_pickup_date)}' if doc.est_pickup_date else '')


def ineligible_reason(doc, lines=None):
    """Why this receipt can't be held for pickup, or '' when it can: a completed sale of goods (not a return or
    exchange, a sales-order pickup, a deposit/payout receipt, or a receipt that only takes a payment on account or sells
    a gift certificate)."""
    if doc.kind != 'sale':
        return 'Hold for Pickup is for sales receipts.'
    if doc.receipt_type not in (None, 0):
        return 'Hold for Pickup is for sales receipts, not deposits, payouts or reversals.'
    if doc.source_order_id:
        return 'This receipt is a pickup from a sales order, so it can\'t be held for pickup.'
    lines = list(doc.lines.all()) if lines is None else lines
    if any(l.returned_from_line_id or l.quantity < 0 for l in lines):
        return 'Hold for Pickup is for sales. A return or exchange can\'t be held for pickup.'
    goods = [l for l in lines if l.quantity > 0 and not l.restocking_fee and not l.certificate_number
             and not (l.item_id is None and l.item_name == accounts.POA_NAME)]
    if not goods:
        return 'Hold for Pickup is for items the customer will collect; this receipt has none.'
    return ''


def parse_date(raw, sale_day):
    """Optional est. pickup date (yyyy-mm-dd from the date box). Not before the sale's day, at most a year ahead."""
    raw = str(raw or '').strip()
    if not raw:
        return None
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        raise PickupError('Enter a valid est. pickup date.')
    if value < sale_day:
        raise PickupError(f'The est. pickup date can\'t be before the sale ({sale_day.month}/{sale_day.day}/{sale_day.year}).')
    if value > store_today() + timedelta(days=366):
        raise PickupError('The est. pickup date must be within a year.')
    return value


def apply_at_checkout(document, lines, data):
    """Checkout (services.save_document, action complete): the "Hold for Pickup" box and its date. Only when the
    register sent the box (`hold_for_pickup` in data); a re-save that unticks it clears the hold and any pickup stamp."""
    if 'hold_for_pickup' not in data:
        return
    if data.get('hold_for_pickup') is not True:
        clear(document)
        return
    reason = ineligible_reason(document, lines)
    if reason:
        raise PickupError(reason)
    sale_day = timezone.localdate(document.completed_at) if document.completed_at else store_today()
    document.hold_for_pickup = True
    document.est_pickup_date = parse_date(data.get('est_pickup_date'), sale_day)


def clear(document):
    document.hold_for_pickup = False
    document.est_pickup_date = None
    document.picked_up_at = None
    document.picked_up_by = ''


PICKUP_FIELDS = ['hold_for_pickup', 'est_pickup_date', 'picked_up_at', 'picked_up_by', 'version']


@transaction.atomic
def act(token, action, est_pickup_date='', cashier=None, version=None):
    """The finished receipt's pickup buttons, any day (no money changes, so no receipt lock or override):
    'hold' = Hold for Pickup (optional date), 'date' = change the est. pickup date, 'picked_up' = Mark Picked Up
    (stamps now and the active cashier). Returns the receipt."""
    doc = Document.objects.select_for_update().filter(token=token, kind='sale').first()
    if doc is None:
        raise PickupError('This receipt no longer exists.')
    if version not in (None, '') and str(doc.version) != str(version):
        raise PickupError('Someone changed this receipt. Reload it and try again.')
    if doc.status != 'completed':
        raise PickupError('Only a completed, paid receipt can be held for pickup.' if doc.status == 'held'
                          else 'This receipt was cancelled, so it can\'t be held for pickup.')
    sale_day = timezone.localdate(doc.completed_at) if doc.completed_at else store_today()
    if action == 'hold':
        if doc.hold_for_pickup:
            raise PickupError('This receipt is already held for pickup.' if not doc.picked_up_at else 'This receipt was already picked up.')
        reason = ineligible_reason(doc)
        if reason:
            raise PickupError(reason)
        doc.hold_for_pickup = True
        doc.est_pickup_date = parse_date(est_pickup_date, sale_day)
        doc.picked_up_at, doc.picked_up_by = None, ''
    elif action == 'date':
        if not doc.awaiting_pickup:
            raise PickupError('This receipt isn\'t awaiting pickup.')
        doc.est_pickup_date = parse_date(est_pickup_date, sale_day)
    elif action == 'picked_up':
        if not doc.awaiting_pickup:
            raise PickupError('This receipt isn\'t awaiting pickup.' if not doc.picked_up_at else 'This receipt was already picked up.')
        doc.picked_up_at = timezone.now()
        doc.picked_up_by = (cashier or '')[:100]
    else:
        raise PickupError('Choose an available action.')
    doc.version += 1
    doc.save(update_fields=PICKUP_FIELDS)
    return doc
