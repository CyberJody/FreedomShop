# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal
from .models import DiscountReason, allocate_receipt_discount, money
from .services import WorkflowError, decimal, short


def reason_fields(data, old=None):
    reason = short(data.get('discount_reason', ''), 100, 'Discount reason')
    note = short(data.get('discount_note', ''), 200, 'Discount note')
    if reason and reason != getattr(old, 'discount_reason', '').replace('Price Descrepancy', 'Price Discrepancy') and not DiscountReason.objects.filter(name=reason, active=True).exists():
        raise WorkflowError('Choose an active discount reason from the list.')
    return reason, note


def _qb_discount_kept(document, percent, amount):
    """QuickBooks recorded no reason for an order discount, so re-saving an imported open order with its
    discount untouched needs none; changing the discount asks for a reason like any app order."""
    if document is None or not getattr(document, 'from_quickbooks', False) or document.kind != 'order' or document.discount_reason:
        return False
    recorded_percent = document.discount_percent or Decimal('0')
    if recorded_percent:
        return percent == recorded_percent and not amount
    return not percent and amount == (document.discount or Decimal('0'))


def qb_typed_discount(document, lines):
    """True when an imported (QuickBooks) order's discount is a typed dollar amount stored as its 2-place percent
    echo: the recorded amount isn't the percent of the eligible lines (13 of the 41 discounted open orders in the
    10/18 data; 3401: 4.88 % of 24,503.65 is 1,195.78, QB recorded 1,196.00). `lines` must be the order's lines AS
    SAVED (before an edit touches them): classifying from edited amounts made genuine percents look typed (a prior
    review, finding 1: 5400's 5 % stayed $344.24 after a quantity edit instead of becoming $485.74). Shared by
    services.save_document and the register's state (views.document_data), so both keep the same amount."""
    if (document is None or not getattr(document, 'from_quickbooks', False) or document.kind != 'order'
            or not document.discount or not document.discount_percent):
        return False
    base = sum((line.amount for line in lines if eligible(line)), Decimal('0'))
    return document.discount != money(base * document.discount_percent / 100)


def eligible(line):
    """Lines a receipt-level discount applies to: merchandise and misc items, not returns, restocking fees,
    gift certificates or payments on account."""
    return not (line.returned_from_line_id or line.restocking_fee or line.certificate_number or line.item_name == 'Payment On Account')


def apply_discounts(data, lines, kind, document, pickup_shares=None, keep_total=None):
    """Receipt-level % discount, QB's way: unit prices stay as rung, each eligible line gets its share of the
    discount (`Line.receipt_discount`, half-up on its extension, leftover cent on the largest line), and the receipt's
    `discount` is the sum. Subtotal stays gross; tax is figured on the discounted line amounts; total = subtotal -
    discount + tax. Returns (discount total, percent, reason, note).

    Sales orders take the same whole-order % discount: all 170 %-discounted orders in the 10/9 QB
    data (168 with an amount) recompute exactly through this allocation and the order's tax math
    (the import parity checks).

    `pickup_shares` ({id(line): amount}, only from services.fulfill_order) carries the order lines' own shares onto a
    pickup receipt instead of re-allocating: a partial pickup takes exactly the share of what is picked up, so the
    pickups add up to the order to the cent.

    `keep_total` (only from services.save_document): QuickBooks' recorded discount amount on an imported order
    whose percent is a typed dollar amount's 2-place echo (13 of the 41 discounted open orders in the 10/18 data:
    4.88 % of 24,503.65 is 1,195.78, QB recorded 1,196.00). While the percent is left alone the amount stays QB's and
    the percent only shapes the shares, as the importer spreads it; a changed percent re-applies like an app order."""
    what = 'order' if kind == 'order' else 'receipt'
    percent = decimal(data.get('discount_percent') or '0', 2, f'{what} discount percent', maximum=Decimal('100'))
    # a sales order may take a whole-cent dollar amount instead of a percent. It is stored in the
    # same fields - `discount` = the amount, `discount_percent` = 0 - and spread over the eligible lines in proportion
    # to their extensions (allocate_receipt_discount with the amount as `total`), which is what QB does with a typed
    # dollar receipt discount tax and total exact on every typed-dollar receipt).
    amount = decimal(data.get('discount_amount') or '0', 2, f'{what} discount amount', maximum=Decimal('9999999999.99'))
    if pickup_shares is not None:
        # The order already validated its reason; a reason turned off since then must not block the pickup.
        reason = short(data.get('discount_reason', ''), 100, 'Discount reason')
        note = short(data.get('discount_note', ''), 200, 'Discount note')
        for line in lines:
            line.receipt_discount = pickup_shares.get(id(line), Decimal('0.00'))
        return sum((line.receipt_discount for line in lines), Decimal('0.00')), percent, reason, note
    if amount and kind != 'order':
        raise WorkflowError('A dollar discount is for sales orders; give a receipt discount as a percent.')
    if amount and percent:
        raise WorkflowError('Enter the order discount as a percent or a dollar amount, not both.')
    reason, note = reason_fields(data, document)
    from shop_settings.config import register_rules
    if register_rules().require_discount_reason and (percent or amount) and not reason and not _qb_discount_kept(document, percent, amount):
        raise WorkflowError(f'Choose a reason for the {what} discount.')
    for line in lines:
        line.receipt_discount = Decimal('0.00')
        if line.returned_from_line_id:
            # Returning an item sold on a %-discounted receipt refunds the discounted amount: QB re-applies that
            # receipt's percent to the (negative) return line (store receipts 2015-2026), independent of any discount
            # on the return receipt itself.
            original = line.returned_from_line
            original_percent = original.document.discount_percent or Decimal('0')
            if original_percent:
                line.receipt_discount = money(line.amount * original_percent / 100)
            elif original.receipt_discount and original.amount:
                # Sold on a pickup of a $-discounted order (percent 0): the returned quantity's part of that
                # line's own share, so the refund is what the customer paid for it.
                line.receipt_discount = money(line.amount * original.receipt_discount / original.amount)
    targets = [line for line in lines if eligible(line)]
    if amount:
        base = sum((line.amount for line in targets), Decimal('0'))
        if amount > base:
            raise WorkflowError(f'The ${amount:,.2f} order discount is more than the ${base:,.2f} of items it can come off.')
        shares = allocate_receipt_discount([line.amount for line in targets], Decimal('0'), total=amount)
    elif keep_total is not None and percent:
        base = sum((line.amount for line in targets), Decimal('0'))
        if keep_total > base:
            raise WorkflowError(f'The ${keep_total:,.2f} order discount is more than the ${base:,.2f} of items it can come off.')
        shares = allocate_receipt_discount([line.amount for line in targets], percent, total=keep_total)
    else:
        shares = allocate_receipt_discount([line.amount for line in targets], percent) if percent else []
    for line, share in zip(targets, shares):
        line.receipt_discount = share
    return sum((line.receipt_discount for line in lines), Decimal('0.00')), percent, reason, note


def pickup_share(order_line, quantity):
    """The part of an order line's discount share that a pickup of `quantity` of it carries: the share for
    the quantity picked up (half-up on its extension), and the whole remaining share when this pickup finishes the
    line, so the leftover cent QB put on the largest line travels with that line's last pickup and the pickup
    receipts add up to the order's discount exactly. 0 when the order has no discount."""
    share = order_line.receipt_discount or Decimal('0')
    if not share:
        return Decimal('0.00')
    taken = sum((delivery.receipt_discount or Decimal('0') for delivery in order_line.deliveries.filter(document__status='completed')), Decimal('0'))
    remaining = share - taken
    if quantity >= order_line.quantity - order_line.fulfilled:
        return remaining
    percent = order_line.document.discount_percent or Decimal('0')
    part = money(quantity * order_line.price * percent / 100) if percent else money(share * quantity / order_line.quantity)
    return max(min(part, remaining), Decimal('0.00'))


def imported_order_shares(lines, amount, percent):
    """Spread an imported QB order's recorded discount (DiscAmount) over its lines the way QB spreads a receipt's
    (per-line half-up on the extension, leftover cent on the largest line): QB stores no per-line shares on order
    lines (no RealPrice column), and DiscAmount stays authoritative. Returns {line: share}."""
    targets = [line for line in lines if eligible(line)]
    if not targets or not amount:
        return {line: Decimal('0.00') for line in lines}
    shares = allocate_receipt_discount([line.amount for line in targets], percent or Decimal('0'), total=amount)
    out = {line: Decimal('0.00') for line in lines}
    out.update(zip(targets, shares))
    return out
