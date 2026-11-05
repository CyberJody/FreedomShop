# SPDX-License-Identifier: AGPL-3.0-or-later
"""Atomic, editable test transactions with stock changes applied as differences."""
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from django.db import transaction
from django.utils import timezone
from customers.models import Customer, tax_location_for
from .models import Document, Item, Line, StockMovement, OrderPayment, ReceiptOverride, PAYMENT_METHODS, CARD_TYPES, RETURN_REASONS, RESTOCKING_FEE_NAME, RESTOCKING_FEE_PERCENT, restocking_fees, money, tax_amount, quantity_text, plain, discounted_price
from . import accounts

class WorkflowError(ValueError):
    pass


# Refunds (B1): the cashier chooses how the money goes back; the store refunds by cash, card or charge account.
REFUND_METHODS = ('cash', 'credit', 'account')



def default_line_rate():
    """The last fallback tax rate for a sale line (after the line's own, the customer's and the item's): Tax Settings."""
    from shop_settings.config import tax
    return tax().line_rate

def returnable(line):
    """A receipt line that Accept Return may return: an item sold on a completed sales receipt (not a deposit,
    payout or reversal receipt, not a Payment On Account line, not itself a return)."""
    doc = line.document
    return (doc.kind == 'sale' and doc.status == 'completed' and doc.receipt_type in (None, 0) and line.quantity is not None
            and line.quantity > 0 and not line.certificate_number and line.item_name.upper() != 'GIFT CERTIFICATE' and not (line.item_id is None and line.item_name == accounts.POA_NAME))


def return_status(original, exclude_document=None):
    """(available, already_returned) for a sold receipt line (the cap is the quantity sold minus what
    already came back). Counts completed app returns linked to the line and, on an imported QB receipt, QB's own
    returns: QB links a return to the receipt (DocRefSID), not the line, so QB returns of the same item are pooled
    across that receipt's lines of the item. `exclude_document` = the return receipt being edited (its own lines)."""
    from django.db.models import Q, Sum
    doc = original.document
    sold = original.quantity
    group = {original.pk: sold}
    imported = Decimal('0')
    if doc.from_quickbooks:
        same = Q(item_id=original.item_id) if original.item_id else Q(item__isnull=True, sku=original.sku, item_name=original.item_name)
        group = dict(doc.lines.filter(same, quantity__gt=0).values_list('pk', 'quantity')) or group
        imported = -(Line.objects.filter(same, returned_from_receipt=doc, quantity__lt=0, document__status='completed')
                     .aggregate(q=Sum('quantity'))['q'] or Decimal('0'))
    app = Line.objects.filter(returned_from_line__in=list(group), document__status='completed')
    if exclude_document is not None and exclude_document.pk:
        app = app.exclude(document=exclude_document)
    by_line = defaultdict(Decimal)
    for pk, qty in app.values_list('returned_from_line_id', 'quantity'):
        by_line[pk] -= qty
    own = sold - by_line[original.pk]
    pool = sum(group.values(), Decimal('0')) - sum(by_line.values(), Decimal('0')) - imported
    available = max(Decimal('0'), min(own, pool))
    return available, sold - available


def decimal(value, places, label, minimum=Decimal('0'), maximum=Decimal('99999999')):
    """Parse a posted number with at most `places` decimals. Quantities use places=0 (whole numbers only;
    '2.000' still counts as whole, '2.5' is rejected with a plain message, never rounded)."""
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < minimum or result > maximum or result != result.quantize(Decimal(10) ** -places):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        if places == 0:
            raise WorkflowError(f'{label[:1].upper()}{label[1:]} must be a whole number{" (at least 1)" if minimum >= 1 else ""}.')
        raise WorkflowError(f'Enter a valid {label} (up to {places} decimal places).')


def carried_price(raw, old, any_value=False):
    """The exact stored price when a saved line comes back unchanged with a fractional-cent price (a % discount or a
    held receipt from QuickBooks), else None. With any_value, any finite non-negative number (used to word the
    whole-cents message)."""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value < 0:
        return None
    if any_value:
        return value
    if old is not None and old.price is not None and value == old.price and old.price != old.price.quantize(Decimal('0.01')):
        return old.price
    return None


def short(value, length, label):
    if not isinstance(value, str) or len(value) > length:
        raise WorkflowError(f'{label} must be no more than {length} characters.')
    return value.strip()


def payment_method(value):
    if value not in dict(PAYMENT_METHODS):
        raise WorkflowError('Choose how the customer paid.')
    return value


def kept_line(old, item, row, pickup):
    """Items Trash: may this line keep a trashed item? Yes when it is the line the item was already on (same saved
    line, same item) or a pickup of an order line that holds it.

    `pickup` is server-internal: only `fulfill_order` passes it, as {order line pk: Line} for the lines of the one order
    it holds locked (open, quantity left). A `pickup_line` in a row from the register or the API means nothing on its
    own, so a client can't use an old order line to put a trashed item on an unrelated receipt or order."""
    if old is not None and old.item_id == item.pk:
        return True
    source = (pickup or {}).get(row.get('pickup_line'))
    return source is not None and source.item_id == item.pk


def locked_message(document):
    return (f'{document.numbered_label} was completed on {document.store_day:%-m/%-d/%Y}. A receipt can only be changed on '
            f'the day it was completed. To correct it, use Accept Return.')


def receipt_snapshot(document):
    """What an Admin Override record keeps of the receipt before and after the change (plain JSON)."""
    document.refresh_from_db()
    return dict(status=document.status, customer=document.customer_name, tax_exempt=document.tax_exempt,
                subtotal=plain(document.subtotal), tax=plain(document.tax), total=plain(document.total),
                payments=document.payment_breakdown, notes=document.notes, discount_reason=document.discount_reason, discount_note=document.discount_note, discount_percent=str(document.discount_percent),
                lines=[dict(id=l.pk, item_name=l.item_name or l.sku, description=l.description, quantity=quantity_text(l.quantity),
                            price=plain(l.price), discount_reason=l.discount_reason, discount_note=l.discount_note, tax_rate=str(l.tax_rate) if l.tax_rate is not None else None)
                       for l in document.lines.all()])


def snapshot_summary(before, after):
    """Plain-English list of what an override changed (shown under Override history)."""
    changes = []
    if before['status'] != after['status']:
        changes.append(f"Status {before['status']} -> {after['status']}")
    for key, label in (('customer', 'Customer'), ('subtotal', 'Subtotal'), ('tax', 'Tax'), ('total', 'Total')):
        if before[key] != after[key]:
            money_key = key != 'customer'
            changes.append(f"{label} {'$' if money_key else ''}{before[key]} -> {'$' if money_key else ''}{after[key]}")
    if before['payments'] != after['payments']:
        text = lambda ps: ', '.join(f"{p['method']} ${p['amount']}" for p in ps) or 'none'
        changes.append(f"Payments {text(before['payments'])} -> {text(after['payments'])}")
    if before['tax_exempt'] != after['tax_exempt']:
        changes.append(f"Tax exempt {'yes' if before['tax_exempt'] else 'no'} -> {'yes' if after['tax_exempt'] else 'no'}")
    old = {l['id']: l for l in before['lines']}
    new = {l['id']: l for l in after['lines']}
    for pk, line in new.items():
        was = old.get(pk)
        if was is None:
            changes.append(f"Added {line['quantity']} x {line['item_name']} at ${line['price']}")
        elif (was['quantity'], was['price'], was['tax_rate'], was['description']) != (line['quantity'], line['price'], line['tax_rate'], line['description']):
            changes.append(f"{line['item_name']}: {was['quantity']} x ${was['price']} -> {line['quantity']} x ${line['price']}"
                           + ('' if was['tax_rate'] == line['tax_rate'] else f" (tax {was['tax_rate']}% -> {line['tax_rate']}%)"))
    for pk, line in old.items():
        if pk not in new:
            changes.append(f"Removed {line['quantity']} x {line['item_name']}")
    if any(before.get(k) != after.get(k) for k in ('discount_reason', 'discount_note', 'discount_percent')):
        changes.append('Receipt discount details changed')
    if [(l.get('discount_reason'), l.get('discount_note')) for l in before['lines']] != [(l.get('discount_reason'), l.get('discount_note')) for l in after['lines']]:
        changes.append('Line discount details changed')
    if before['notes'] != after['notes']:
        changes.append('Instructions changed')
    return '; '.join(changes) or 'No changes to items, totals or payments'


def check_override(document, action, override):
    """A locked (past-day) receipt changes only through an Admin Override with a typed reason. Returns the
    'before' snapshot to log, or None when the receipt is not locked."""
    if not document or not document.locked:
        return None
    if override is None:
        raise WorkflowError(locked_message(document))
    reason = override.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise WorkflowError('Enter the reason for this Admin Override before saving.')
    if len(reason) > 2000:
        raise WorkflowError('Override reason must be no more than 2000 characters.')
    if action == 'hold':
        raise WorkflowError('A past receipt cannot be put on hold. Save the override or cancel the receipt.')
    return receipt_snapshot(document)


def log_override(document, action, override, before):
    if before is None:
        return
    after = receipt_snapshot(document)
    ReceiptOverride.objects.create(document=document, action='cancel' if action == 'cancel' else 'save',
                                   reason=override['reason'].strip(), performed_by=str(override.get('by') or '')[:150],
                                   client_address=str(override.get('address') or '')[:64],
                                   before=before, after=after, summary=snapshot_summary(before, after))


def qb_order_guard(order, operation):
    """review, findings 2 and 4: imported (QuickBooks) open orders the app can't yet work safely. `operation` is
    'edit' (a money edit through the register), 'pickup', or 'refund' (cancel, or a negative order payment).
    Payments always stay allowed, and cancel on a shipping order (its refund covers the whole deposit)."""
    if not order.from_quickbooks or order.kind != 'order':
        return
    other = order.qb_other_charge
    if other and operation in ('edit', 'pickup'):
        raise WorkflowError(f'This order carries a ${other:,.2f} shipping/other charge from QuickBooks that the app cannot keep yet. '
                            'Payments and Cancel still work; ask the office to finish this order.')
    mismatch = order.qb_deposit_mismatch
    if mismatch and operation in ('pickup', 'refund'):
        taken, linked = mismatch
        raise WorkflowError(f'QuickBooks shows a ${taken:,.2f} deposit on this order, but its QuickBooks deposit receipts add up to ${linked:,.2f}. '
                            'Pickups, refunds and Cancel are stopped on this order until the office checks it against QuickBooks; payments can still be recorded.')


def recalculate_order(order, after_refund=False):
    order.refresh_from_db()
    if order.status != 'cancelled':
        order.status = 'completed' if order.lines.exists() and order.unfilled == 0 and order.balance_due == 0 else 'open'
        order.version += 1
        order.save(update_fields=['status', 'version'])
    # (F3): once the order is completed, cancelled or its balance is exactly $0, the tax booked on its
    # payments over its life must equal its final tax; any difference is booked today as a $0 adjustment row.
    # `after_refund` (part 5): a refund that leaves the order paid in full or overpaid settles it too.
    from reports.tax_ledger import settle_order_tax
    settle_order_tax(order, after_refund=after_refund)


@transaction.atomic
def save_document(data, kind, token, override=None, cashier=None, pickup=None):
    """Save, hold, complete or cancel a receipt/order. `override` = dict(reason, by, address) only from a signed-in admin:
    needed (with a reason) to change a receipt after its store day, and logged as a ReceiptOverride.
    `pickup` = {order line pk: Line}, passed only by `fulfill_order` (never from a request): the order lines this
    receipt picks up, so their items may be used even when they are in the Items Trash."""
    try: token = uuid.UUID(str(token))
    except (ValueError, TypeError): raise WorkflowError('Invalid document. Reload this page.')
    # Lock parent before child for order receipt edits and fulfillment.
    source = Document.objects.filter(token=token).values_list('source_order_id', flat=True).first()
    parent = Document.objects.select_for_update().get(pk=source) if source else None
    document = Document.objects.select_for_update().filter(token=token).first()
    action = data.get('action')
    if action not in ({'save', 'cancel'} if kind == 'order' else {'hold', 'complete', 'cancel'}):
        raise WorkflowError('Choose an available action.')
    if document:
        if document.kind != kind: raise WorkflowError('Document type does not match.')
        if document.imported: raise WorkflowError('Documents imported from QuickBooks are read-only.')
        if str(document.version) != str(data.get('version')):
            raise WorkflowError('Someone changed this document. Reload it before saving again.')
        if document.from_quickbooks and document.status == 'held':
            # A held receipt brought over from QuickBooks becomes an app receipt once it is worked on here.
            document.source_id = None
    elif action == 'cancel': raise WorkflowError('This document has not been saved.')
    before = check_override(document, action, override)
    if document and kind == 'sale':
        # a cancelled receipt is finished (no cancel, hold or save, even with an Admin Override: saving would bring
        # it back as a sale), and a completed receipt can't be put back on hold. The screen hides these buttons too.
        if document.status == 'cancelled':
            raise WorkflowError('This receipt was cancelled. Start a new receipt instead.')
        if document.status == 'completed' and action == 'hold':
            raise WorkflowError('A completed receipt cannot be put on hold.')
    if kind == 'sale' and action == 'hold' and data.get('hold_for_pickup') is True:
        # Hold for Pickup is a paid, finished sale; Put on Hold (an unpaid held receipt) doesn't combine with it.
        raise WorkflowError('Hold for Pickup is for a paid sale. Untick it to Put on Hold, or take payment and save the sale.')
    if document and kind == 'order' and document.status == 'cancelled':
        # (matching QB): a cancelled order is finished, like a cancelled receipt. A save used to bring it
        # back as an Open order, and a second Cancel ran the cancel steps again.
        raise WorkflowError(f'{document.numbered_label} was cancelled. It can no longer be changed.')
    old_customer_id = document.customer_id if document else None
    # the order's Hold for Pickup box changes only on an open (or new) order; no money, tax or stock effect.
    order_pickup = None
    if kind == 'order' and 'order_pickup' in data and (document is None or document.status == 'open'):
        if not isinstance(data['order_pickup'], bool):
            raise WorkflowError('Hold for Pickup must be ticked or unticked.')
        order_pickup = data['order_pickup']
    was_held = bool(document and document.status == 'held')  # a re-saved held receipt may keep its hold time
    previous = list(document.lines.all()) if document else []
    old_lines = {str(line.pk): line for line in previous}
    old_money = {str(line.pk): (line.amount, line.receipt_discount, line.tax_rate) for line in previous}  # before the rows below edit them in place
    old_text = {str(line.pk): (line.item_id, line.item_name, line.description) for line in previous}
    from .discounts import qb_typed_discount
    typed_discount = kind == 'order' and qb_typed_discount(document, previous)  # from the lines as saved, not as edited
    old_qty = defaultdict(Decimal)
    if document and document.status == 'completed' and kind == 'sale':
        for line in previous:
            # A damaged return never went back into stock, so it has nothing to undo.
            if line.item_id and line.item.item_type == 'inventory' and not line.damaged: old_qty[line.item_id] += line.quantity
    if action == 'cancel':
        if kind == 'order': qb_order_guard(document, 'refund')
        if kind == 'order' and document.unused_payments > 0:
            held = document.unused_payments
            method = data.get('deposit_refund_method')
            if data.get('refund_deposit') is not True or method not in REFUND_METHODS:
                raise WorkflowError(f'This order holds ${held:,.2f} in unused deposits. Refund the deposit by Cash, Credit card or Account before cancelling.')
            amount = decimal(data.get('deposit_refund_amount'), 2, 'deposit refund amount')
            if amount != held:
                raise WorkflowError('The unused deposit changed. Reload and confirm the refund amount again.')
            if method == 'account':
                customer = Customer.objects.select_for_update().get(pk=document.customer_id)
                if not customer.charge_account:
                    raise WorkflowError('Turn on Charge Account in Edit Customer before refunding to Account.')
            refund = OrderPayment.objects.create(order=document, amount=-held, method=method, created_at=timezone.now(),
                                                 reference='Deposit refund on cancellation',
                                                 cashier=(cashier or '')[:100])  # who refunded it
            from reports.tax_ledger import lock_payment
            # (F2): settles the order's booked tax to the tax on what was picked up (nothing when nothing was).
            lock_payment(document, refund, cancelled=True)
        returned = Line.objects.filter(returned_from_line__document=document).exclude(document=document).exclude(document__status='cancelled').select_related('document').first()
        if returned:
            raise WorkflowError(f'Items on this receipt were returned on {returned.document.numbered_label}. Cancel that return first.')
        from .gifts import apply
        apply(document, [], [], active=False)
        for item in Item.objects.select_for_update().filter(pk__in=old_qty).order_by('pk'):
            if item.on_hand is not None: item.on_hand += old_qty[item.pk]
            item.version += 1; item.save(update_fields=['on_hand','version'])
        StockMovement.objects.filter(document=document).delete()
        if kind=='sale':
            from .pickup import clear
            clear(document)  # a cancelled receipt is not awaiting pickup
        document.status='cancelled';document.version+=1;document.save()
        accounts.refresh(old_customer_id)
        if parent: recalculate_order(parent)
        if kind=='order':
            # (F3): a cancelled order's booked tax must end at the tax on what was picked up.
            from reports.tax_ledger import settle_order_tax
            settle_order_tax(document)
        log_override(document, action, override, before)
        from reports.exchange_corrections import capture_cancellation
        try:
            capture_cancellation(document)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        return document
    rows=data.get('lines')
    if not isinstance(rows,list) or not 1<=len(rows)<=100 or any(not isinstance(r,dict) for r in rows):
        raise WorkflowError('Add between 1 and 100 items before saving.')
    key=str(data.get('customer_id') or '')
    customer=None
    if key.isdigit() and len(key)<=18:
        # Lock the old and new customers together in pk order (review): a receipt switching customers at completion
        # must not wait on another register that holds the old customer, while that register waits on the counter we hold.
        lock_ids=sorted({int(key)}|({old_customer_id} if old_customer_id else set()))
        list(Customer.objects.select_for_update().filter(pk__in=lock_ids).order_by('pk'))
        customer=Customer.objects.filter(pk=key,active=True).first()
    if not customer: raise WorkflowError('A customer is required on every receipt and sales order.')
    ids=set(old_qty)
    for row in rows:
        if row.get('item_id'):
            key=str(row['item_id'])
            if not key.isdigit() or len(key)>18: raise WorkflowError('Choose a valid inventory item.')
            ids.add(int(key))
    # Accept Return (B1): return lines point at the receipt line they return; lock those lines and their items.
    originals={}
    for row in rows:
        old=old_lines.get(str(row.get('id')))
        key=old.returned_from_line_id if old else row.get('returned_from_line')
        if key in (None,''): continue
        if kind!='sale': raise WorkflowError('Returns are taken on a sales receipt, not a sales order.')
        if parent: raise WorkflowError('Returns cannot be added to a pickup receipt. Start a new receipt with Accept Return.')
        key=str(key)
        if not key.isdigit() or len(key)>18: raise WorkflowError('Choose a valid receipt line to return.')
        originals[int(key)]=bool(old)
    if originals:
        found={l.pk:l for l in Line.objects.select_for_update(of=('self',)).select_related('document').filter(pk__in=originals).order_by('pk')}
        for pk,saved in list(originals.items()):
            original=found.get(pk)
            if not original or (not saved and (not returnable(original) or (document and original.document_id==document.pk))):
                raise WorkflowError('That receipt line can no longer be returned. Reload and choose the items again.')
            originals[pk]=original
            if original.item_id: ids.add(original.item_id)
    items={i.pk:i for i in Item.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    exempt=data.get('tax_exempt', customer.tax_exempt if not document else document.tax_exempt) is True
    lines=[]; quantities=defaultdict(Decimal);seen=set();returned=defaultdict(Decimal);pickup_shares={}
    for position,row in enumerate(rows):
        if row.get('restocking_fee') is True: continue  # fee lines are recomputed below from the return lines
        old=old_lines.get(str(row.get('id')))
        if row.get('id') and not old: raise WorkflowError('This line no longer belongs to the document. Reload before saving.')
        if old and old.pk in seen: raise WorkflowError('An item line was submitted twice.')
        if old: seen.add(old.pk)
        on_account=row.get('account_payment') is True
        key=old.returned_from_line_id if old else row.get('returned_from_line')
        original=originals.get(int(key)) if key not in (None,'') else None
        if original and on_account: raise WorkflowError('A return line cannot be a payment on account.')
        if original:
            # Returned at the original line's price and tax rate (QB Accept Return); the quantity is negative.
            item=items.get(original.item_id) if original.item_id else None
            qty=decimal(row.get('quantity'),0,'return quantity',minimum=Decimal('-99999999'))
            if qty>=0: raise WorkflowError(f'Enter the return quantity for {original.item_name or original.description} as a negative number, e.g. -1.')
            returned[original.pk]-=qty
            rate=original.tax_rate if original.tax_rate is not None else (customer.tax_rate if customer.tax_rate is not None else (item.tax_rate if item and item.tax_rate is not None else default_line_rate()))
            line=old or Line(returned_from_line=original)
            line.position=position;line.item=item;line.quantity=qty;line.price=original.price;line.tax_rate=rate;line.recorded_amount=None
            line.sku=original.sku;line.item_name=original.item_name;line.description=original.description or original.item_name or 'Returned item'
            line.vendor=original.vendor
            line.damaged=row.get('damaged') is True  # "Damaged — don't restock": no stock comes back
            reason=row.get('return_reason') or ''
            if reason not in dict(RETURN_REASONS) and reason!='': raise WorkflowError('Choose a return reason from the list.')
            line.return_reason=reason;line.return_note=short(row.get('return_note') or '',200,'Return note')
            if item and item.item_type == 'inventory' and not line.damaged: quantities[item.pk]+=qty
            if action=='complete' and (not old or document.status!='completed'):
                line.unit_cost=original.unit_cost if original.unit_cost is not None else (item.average_cost if item else None)
            lines.append(line)
            continue
        if on_account and (kind!='sale' or row.get('item_id')): raise WorkflowError('Payments on account can only be taken on a sales receipt.')
        item=items.get(int(row['item_id'])) if row.get('item_id') else None
        if row.get('item_id') and not item: raise WorkflowError('Choose an active inventory item.')
        if item and not item.active and not kept_line(old, item, row, pickup):
            # Items Trash: a trashed item stays on the lines it was already on (re-saving an open
            # order or held receipt, picking up an order) but can't be added to anything new.
            raise WorkflowError(f'{item.display_name} is in the Items Trash. Restore it from the Items Trash (admin sign-in) before adding it to a new line.')
        if not on_account and str(row.get('item_name','')).strip().lower()==accounts.POA_NAME.lower(): raise WorkflowError('"Payment On Account" is reserved. Use Take Payment for Charge Account.')
        if on_account:
            row={**row,'quantity':'1','tax_rate':'0','sku':'','item_name':accounts.POA_NAME,'description':accounts.POA_NAME,'vendor':''}
        qty=decimal(row.get('quantity'),0,'quantity',Decimal('1'))  # whole numbers only
        line=old or Line()
        # % off: a line % is ALWAYS taken off the item's
        # regular price and replaces any price typed on the line — in the 10/9 data all 1,103 line-% lines are regular
        # price x (1 - %), regular = the item's price. The register sends the base as `original_price`; for an inventory
        # item it must be the item's regular price (the line's stored base on a saved line, else the catalog price) —
        # a typed price is never the base. A misc line has no catalog price, so its base is the price first rung.
        # A typed price with no % is whole cents; a saved line that comes back unchanged keeps its exact
        # stored price (e.g. 4-decimal prices on held receipts from QuickBooks).
        percent=decimal(row.get('discount_percent') or '0', 2, 'line discount percent', maximum=Decimal('100')) if not on_account else Decimal('0')
        # or a dollar amount off each unit (QB's Unit Discount; price = regular - $, so qty x $ comes off the line).
        # Stored like a price typed below regular (discount_percent 0, original_price = regular): QB treats the two alike.
        dollars_off=decimal(row.get('discount_amount') or '0', 2, 'line discount amount', maximum=Decimal('9999999999.99')) if not on_account else Decimal('0')
        if percent and dollars_off: raise WorkflowError('Enter a line discount as a percent or a dollar amount, not both.')
        raw_price=str(row.get('price','')).strip()
        raw_regular=str(row.get('original_price','') if row.get('original_price') is not None else '').strip()
        client_regular=decimal(raw_regular,2,'regular price') if raw_regular and not on_account else None
        catalog_regular=(old.original_price if old and old.original_price is not None else item.price) if item else None
        source_line=(pickup or {}).get(row.get('pickup_line'))
        if source_line is not None:
            # A pickup rings the order line as it was sold: its exact price (4 decimals after a line %), its
            # regular price and % off, whatever the catalog price is today. Only fulfill_order passes `pickup`.
            regular=source_line.original_price;percent=source_line.discount_percent or Decimal('0');price=source_line.price
        elif catalog_regular is not None:
            if (percent or dollars_off) and client_regular is not None and client_regular!=catalog_regular:
                # The register shows a % off the price it had when the item was picked; never save a different amount.
                raise WorkflowError(f"The regular price of {item.display_name if item else 'this item'} is now ${catalog_regular:,.2f}. Remove the line and add the item again before giving {'a % off' if percent else 'a dollar discount'}.")
            regular=catalog_regular
        else:
            regular=client_regular
        if source_line is not None:
            pass
        elif percent:
            if regular is None:
                regular=decimal(raw_price,2,'regular price')
            price=discounted_price(regular,percent)
        elif dollars_off:
            if regular is None:
                regular=decimal(raw_price,2,'regular price')
            if dollars_off>regular:
                name=row.get('item_name') or (item.display_name if item else 'this item')
                raise WorkflowError(f'The ${dollars_off:,.2f} discount on {name} is more than its ${regular:,.2f} price.')
            price=regular-dollars_off
        else:
            price=carried_price(raw_price,old)
            if price is None:
                try: price=decimal(raw_price,2,'price')
                except WorkflowError:
                    if carried_price(raw_price,None,any_value=True) is not None: raise WorkflowError('Enter the price in whole cents (two decimal places). Use the % off field for a discount that leaves a fraction of a cent.')
                    raise
        raw_rate=row.get('tax_rate', old.tax_rate if old else (customer.tax_rate if customer.tax_rate is not None else (item.tax_rate if item else default_line_rate())))
        rate=decimal(raw_rate if raw_rate is not None else default_line_rate(),3,'tax rate',maximum=Decimal('100'))
        line.position=position;line.item=item;line.quantity=qty;line.price=price;line.tax_rate=rate;line.recorded_amount=None;line.damaged=False;line.return_reason='';line.return_note=''
        if source_line is not None and row.get('recorded_amount') is not None: line.recorded_amount=decimal(row['recorded_amount'],2,'line total')  # last part of a split order line (fulfill_order)
        line.original_price=regular if regular is not None else price
        line.discount_percent=percent
        from .discounts import reason_fields
        if source_line is not None: line.discount_reason, line.discount_note = source_line.discount_reason, source_line.discount_note  # as sold on the order
        else: line.discount_reason, line.discount_note = reason_fields(row, old)
        line.sku=short(row.get('sku',item.sku if item else ''),100,'Item number')
        line.item_name=short(row.get('item_name',item.display_name if item else line.sku),200,'Item name')
        line.description=short(row.get('description',item.description if item else ''),1000,'Description')
        line.vendor=short(row.get('vendor',item.vendor if item else ''),200,'Vendor')
        line.certificate_number = short(row.get('certificate_number', ''), 100, 'Gift certificate number')
        is_certificate = line.certificate_number or line.item_name.upper() == 'GIFT CERTIFICATE' or (item and item.display_name.upper() == 'GIFT CERTIFICATE')
        if is_certificate:
            if item and item.display_name.upper() != 'GIFT CERTIFICATE':
                raise WorkflowError('Use Sell Gift Certificate, not a merchandise item, to issue a certificate.')
            if kind != 'sale' or on_account or not line.certificate_number:
                raise WorkflowError('Sell gift certificates on a receipt with a certificate number.')
            if qty != 1 or price <= 0:
                raise WorkflowError('Each gift certificate must have quantity 1 and an amount greater than zero.')
            line.item_name = 'GIFT CERTIFICATE'
            line.tax_rate = Decimal('0')
        if not line.description: raise WorkflowError('Enter an item description.')
        if on_account and price<=0: raise WorkflowError('Enter the payment on account amount.')
        if item and item.item_type == 'inventory': quantities[item.pk]+=qty
        if action=='complete' and (not old or document.status!='completed'): line.unit_cost=item.average_cost if item else None
        lines.append(line)
        if source_line is not None: pickup_shares[id(line)]=decimal(row.get('receipt_discount') or '0',2,'discount share',minimum=Decimal('-9999999999.99'))
        elif old is not None and old.source_order_line_id and document is not None and document.source_order_id:
            # Re-saving a pickup receipt (same day, e.g. a note): each order line keeps the share the pickup carried;
            # re-allocating by percent would drop a $ order discount (percent 0) and change the total.
            pickup_shares[id(line)]=old.receipt_discount or Decimal('0.00')
    for pk,count in returned.items():
        original=originals[pk]
        available,prior=return_status(original,document)
        if count>available:
            # Cap = quantity sold on that receipt minus what already came back.
            name=original.item_name or original.description
            if prior:
                raise WorkflowError(f'Only {quantity_text(available)} of {name} can still be returned from {original.document.numbered_label}: '
                                    f'{quantity_text(prior)} of the {quantity_text(original.quantity)} sold already came back.')
            raise WorkflowError(f'Only {quantity_text(original.quantity)} of {name} sold on {original.document.numbered_label}. Return no more than that.')
    if not lines: raise WorkflowError('Add between 1 and 100 items before saving.')
    from .discounts import apply_discounts
    pickup_receipt=pickup is not None or bool(document is not None and document.source_order_id)
    keep_total=None
    if typed_discount and not pickup_receipt:
        # QuickBooks' order discount was a typed dollar amount (classified above from the lines as saved, before
        # this edit touched them); while the register sends its percent back unchanged, QB's amount stays — also
        # across a quantity edit, as a dollar discount does (see discounts.apply_discounts `keep_total`). A genuine
        # percent is never kept: it re-applies to the new lines (5400: 5 % of 9,714.70 = 485.74).
        try:
            sent=decimal(data.get('discount_percent') or '0',2,'order discount percent',maximum=Decimal('100'))
            sent_amount=decimal(data.get('discount_amount') or '0',2,'order discount amount')
        except WorkflowError: sent=sent_amount=None   # apply_discounts words the error
        if sent is not None and sent==document.discount_percent and not sent_amount:
            keep_total=document.discount
    receipt_discount, discount_percent, discount_reason, discount_note = apply_discounts(data, lines, kind, document, pickup_shares if pickup_receipt else None, keep_total=keep_total)
    if data.get('restocking_fee') is True:
        # Optional restocking fee (cashier's choice, off by default): its own taxable line(s) on the return receipt,
        # RESTOCKING_FEE_PERCENT of the returned merchandise at what it actually sold for — the line less its share of
        # the original receipt's discount (net_amount), so a $100 item from a 10 %-off receipt is charged 20 % of $90
        # (Review finding 3). Computed after apply_discounts, which sets those shares; fee lines take no
        # share of a receipt discount themselves (discounts.eligible).
        returned_lines=[l for l in lines if l.returned_from_line_id]
        if not returned_lines: raise WorkflowError('Add the returned items before applying a restocking fee.')
        pct=f'{RESTOCKING_FEE_PERCENT.normalize():f}'
        for fee,rate in restocking_fees((l.net_amount,l.tax_rate) for l in returned_lines):
            lines.append(Line(position=len(lines),item=None,sku='',item_name=RESTOCKING_FEE_NAME,description=f'{RESTOCKING_FEE_NAME} ({pct}% of returned items)',
                              vendor='',quantity=Decimal('1'),price=fee,tax_rate=rate,restocking_fee=True,receipt_discount=Decimal('0.00')))
    # QB's receipt arithmetic: Subtotal = gross extensions (each rounded to the cent), Discount taken off, tax per rate
    # on the UNROUNDED extensions less discount shares (Line.tax_basis), rounded half-up once; Total = Subtotal -
    # Discount + Tax.
    subtotal=sum((l.amount for l in lines),Decimal('0'))
    tax=tax_amount(((l.tax_basis,l.tax_rate) for l in lines),exempt)
    if pickup is not None and data.get('pickup_tax') is not None:
        # The last pickup of an order takes the order's tax less what earlier pickups carried (fulfill_order), so the
        # pickup receipts add up to the order to the cent; each receipt rounding its own tax could leave the order a
        # cent short or (worse) a cent over its payments and refuse the pickup.
        tax=decimal(data['pickup_tax'],2,'pickup tax',minimum=Decimal('-9999999999.99'))
    elif pickup is None and pickup_receipt and document.tax is not None and exempt==document.tax_exempt and len(lines)==len(old_money) and all(
            l.pk and old_money.get(str(l.pk))==(l.amount,l.receipt_discount,l.tax_rate) for l in lines):
        # A re-saved pickup receipt with the same lines keeps the tax the pickup squared with its order.
        tax=document.tax
    total=subtotal-receipt_discount+tax
    if kind=='order' and document is not None and document.from_quickbooks and document.total is not None and action=='save':
        # a QuickBooks order keeps QB's recorded
        # subtotal / discount / tax / total until its first money edit — a re-save for a note, a due date or a payment
        # must not move a balance the customer was quoted (4 open orders carry a QB shipping charge the lines don't).
        # From the first edit on it is recomputed like an app order, and the edited lines stop being QB's lines
        # (source_id cleared, so `backfill_unit_prices` never puts QB's figures back over what staff rang).
        unchanged=(exempt==document.tax_exempt and len(lines)==len(old_money) and receipt_discount==(document.discount or Decimal('0'))
                   and discount_percent==(document.discount_percent or Decimal('0'))
                   and all(l.pk and old_money.get(str(l.pk))==(l.amount,l.receipt_discount,l.tax_rate) for l in lines))
        if unchanged:
            subtotal,receipt_discount,tax,total=document.subtotal,document.discount,document.tax,document.total
        else:
            # Review finding 2: a shipping/other charge the app can't keep refuses the edit (a note-only re-save above
            # is always fine). Tax already reported on earlier deposits never blocks an edit: each
            # payment's tax is locked on its own day and the difference is booked when the balance is paid.
            qb_order_guard(document, 'edit')
            for l in lines:
                if l.source_id and old_money.get(str(l.pk))!=(l.amount,l.receipt_discount,l.tax_rate): l.source_id=None
    if total>Decimal('9999999999.99'): raise WorkflowError('The document total is too large.')
    method='';tendered=None; breakdown=[]
    if action=='complete':
        if parent:
            method='order';tendered=total
        else:
            entries=data.get('payments')
            if entries is None:
                entries=[{'method':data.get('payment_method'),'amount':data.get('tendered')}]
            if not isinstance(entries,list) or len(entries)>10 or (not entries and total!=0):
                raise WorkflowError('Enter between 1 and 10 payment amounts.')
            # A return that nets below zero is a refund (B1): amounts are negative (money given back) and must add up
            # to the refund exactly; nothing is given as change. An even exchange (total 0) needs no payment.
            refund=total<0
            tendered=Decimal('0');cash=Decimal('0')
            for entry in entries:
                if not isinstance(entry,dict): raise WorkflowError('Enter a valid payment.')
                if total==0 and not entry.get('method') and str(entry.get('amount') or '0').strip() in {'0','0.0','0.00','-0','-0.00'}:
                    continue
                entry_method=payment_method(entry.get('method'))
                if refund:
                    if entry_method not in REFUND_METHODS: raise WorkflowError('Give the refund as Cash, Credit card or Account.')
                    amount=decimal(entry.get('amount'),2,'refund amount',minimum=Decimal('-9999999999.99'),maximum=Decimal('0'))
                else:
                    amount=decimal(entry.get('amount'),2,'amount received',maximum=Decimal('9999999999.99'))
                tendered+=amount
                if entry_method=='cash': cash+=amount
                detail = {'method': entry_method, 'amount': str(amount)}
                if entry_method == 'credit':
                    from .models import CARD_TYPES
                    name = short(entry.get('tender_name', ''), 100, 'Card type')
                    if name and name not in CARD_TYPES:
                        raise WorkflowError('Choose Visa, MasterCard, American Express or Discover.')
                    if name: detail['tender_name'] = name
                if entry_method in ('check', 'gift'):
                    reference = short(entry.get('reference', ''), 100, 'Payment reference')
                    if reference: detail['reference'] = reference
                breakdown.append(detail)
            if refund:
                if tendered!=total: raise WorkflowError(f'The refund amounts must add up to the refund due, ${-total:,.2f}.')
            else:
                if tendered<total: raise WorkflowError('Amount received is less than the total.')
                if tendered-total>cash: raise WorkflowError('Only cash can be returned as change. Enter the exact amount for other payment types.')
            if tendered>Decimal('9999999999.99'): raise WorkflowError('The payment total is too large.')
            method=(breakdown[0]['method'] if len(breakdown)==1 else 'split') if breakdown else ''
            charge=sum((Decimal(e['amount']) for e in breakdown if e['method']=='account'),Decimal('0'))
            pays=any(l.item_id is None and l.item_name==accounts.POA_NAME for l in lines)
            if pays and charge and not any(l.item_id or l.item_name!=accounts.POA_NAME for l in lines): raise WorkflowError('Pay a payment on account with cash, check, card or another tender, not Account.')
            if charge and pays: raise WorkflowError('A receipt cannot both charge to the account and take a payment on account.')
            if charge and not customer.charge_account:
                raise WorkflowError(f'{customer.display_name} is not set up as a charge account. Turn on Charge Account in Edit Customer first.')
            if charge>0 and customer.account_limit:
                prior=accounts.effect(document) if document and document.customer_id==customer.pk else Decimal('0')
                new_balance=customer.account_balance-prior+charge
                if new_balance>customer.account_limit and data.get('override_limit') is not True:
                    raise WorkflowError(f'This charge brings {customer.display_name} to ${new_balance:,.2f}, over the ${customer.account_limit:,.2f} account limit.')
    if parent:
        other_total=sum(parent.fulfillments.filter(status='completed').exclude(pk=document.pk).values_list('total',flat=True),Decimal('0'))
        if action=='complete' and other_total+total+(parent.deposit_used or Decimal('0'))>parent.paid: raise WorkflowError('Record the remaining payment on the sales order before completing this pickup.')  # deposit QB used: review finding 5
        for line in lines:
            if line.source_order_line_id:
                source_line=Line.objects.get(pk=line.source_order_line_id)
                others=source_line.deliveries.filter(document__status='completed').exclude(document=document)
                if line.quantity+sum(others.values_list('quantity',flat=True),Decimal('0'))>source_line.quantity:
                    raise WorkflowError('Pickup quantity exceeds the customer order. Edit the order first.')
    new_qty=quantities if action=='complete' else {}
    for pk in set(old_qty)|set(new_qty):
        item=items[pk]
        delta=old_qty[pk]-new_qty.get(pk,Decimal('0'))
        if delta and item.on_hand is None: raise WorkflowError(f'Enter a stock quantity for {item.display_name} in Item List first.')
        if delta and abs(item.on_hand+delta)>Decimal('999999999.999'): raise WorkflowError('The resulting stock quantity is too large.')
        if delta:
            item.on_hand+=delta;item.version+=1;item.save(update_fields=['on_hand','version'])
    if not document: document=Document(token=token,kind=kind)
    else: document.version+=1
    document.customer=customer;document.customer_name=customer.display_name
    document.notes=short(data.get('notes',''),5000,'Instructions')
    document.shipping_address=short(data.get('shipping_address',customer.default_shipping_address if not document.pk else document.shipping_address),2000,'Shipping address')
    document.customer_po=short(data.get('customer_po',''),100,'Customer PO number')
    try: document.due_date=date.fromisoformat(data['due_date']) if data.get('due_date') else None
    except (ValueError,TypeError): raise WorkflowError('Enter a valid due date.')
    document.tax_exempt=exempt;document.tax_location=tax_location_for(exempt);document.subtotal=subtotal;document.tax=tax;document.total=total
    document.discount=receipt_discount;document.discount_percent=discount_percent;document.discount_reason=discount_reason;document.discount_note=discount_note
    document.payment_method=method;document.tendered=tendered;document.payment_breakdown=breakdown
    # Identity comes only from the authenticated session, never document JSON.
    # No active cashier leaves existing (including imported QB held) attribution intact.
    # (matching QB): putting a sale on hold records who held it, like QB's held receipts; completing it later
    # stamps the completing cashier instead.
    if cashier is not None and (action in ('complete', 'hold') or (kind == 'order' and action == 'save')):
        document.cashier = cashier
    if action=='complete': document.was_completed=True
    document.status='completed' if action=='complete' else ('open' if kind=='order' else 'held')
    document.completed_at=(document.completed_at or timezone.now()) if action=='complete' else None
    if kind=='sale':
        # a hold for a real customer reserves the items until its hold time runs out; finishing the receipt sells
        # them through the stock movements above (no second deduction), so the hold fields are cleared.
        from . import holds
        if action=='hold':
            try: holds.apply_hold(document,customer,data.get('hold_until'),was_held=was_held)
            except holds.HoldError as exc: raise WorkflowError(str(exc))
        else: holds.clear_hold(document)
        # the checkout's "Hold for Pickup" box and optional est. pickup date (no money, tax or stock effect).
        from . import pickup as hold_pickup
        if action=='complete':
            if parent is not None and data.get('hold_for_pickup') is True:
                raise WorkflowError('This receipt is a pickup from a sales order, so it can\'t be held for pickup.')
            try: hold_pickup.apply_at_checkout(document,lines,data)
            except hold_pickup.PickupError as exc: raise WorkflowError(str(exc))
        else: hold_pickup.clear(document)
    elif order_pickup is not None:
        document.order_pickup = order_pickup
    document.save()
    from .gifts import apply
    apply(document, lines, breakdown, active=action == 'complete')
    # Preserve line identifiers so vendor procurement and pickup links survive edits.
    for line in lines:
        line.document=document;line.save()
    removed=document.lines.exclude(pk__in=[l.pk for l in lines])
    returned_line=Line.objects.filter(returned_from_line__in=removed).select_related('document').first()
    if returned_line:
        raise WorkflowError(f'{returned_line.item_name or returned_line.description} was returned on {returned_line.document.numbered_label}, so it cannot be removed from this receipt.')
    removed.delete()
    if kind=='order':
        # (matching QB): a special-order item that belongs to this order follows the line's new name/description
        # (and so do its not-yet-received PO lines); `notes` tell staff what else changed.
        from .special_items import sync_special_items
        document.notes_for_screen=sync_special_items(document, lines, old_text)
    StockMovement.objects.filter(document=document).delete()
    if action=='complete':
        for pk,qty in quantities.items(): StockMovement.objects.create(document=document,item_id=pk,quantity=-qty)
    accounts.refresh(old_customer_id,customer.pk)
    if parent: recalculate_order(parent)
    if kind=='order': recalculate_order(document);document.refresh_from_db()
    log_override(document, action, override, before)
    return document


EARLIER_DAY_MESSAGE = 'This payment is from an earlier day. To change it, record a refund or a new payment today.'
DATED_TODAY_MESSAGE = 'A payment is dated the day it is recorded. To put money on another day, record a refund or a new payment today.'
ADJUSTMENT_MESSAGE = 'This is a sales tax adjustment the app booked when the order settled. It cannot be changed or deleted.'
CHECK_NUMBER = re.compile(r'[A-Za-z0-9][A-Za-z0-9 -]*')   # "1470", "A-1470"


def overpayment_message(over, balance):
    """The warning the page shows and the server repeats: a payment that takes more than the
    balance due is allowed, but only once the cashier has said so."""
    return f'This is ${over:,.2f} more than the ${balance:,.2f} balance due. Take the overpayment anyway?'


def payment_snapshot(payment):
    """What a correction or delete can change on an order payment, for the change log."""
    return dict(amount=str(money(payment.amount)), method=payment.method, card_type=payment.card_type, check_number=payment.check_number, reference=payment.reference, date=payment.store_day.isoformat(),
                tendered=str(money(payment.tendered)) if payment.tendered is not None else '')


def log_payment_change(order, payment, action, before, after, actor):
    """Append-only OrderPaymentChange: who (signed-in login, active cashier, address), what changed, the order."""
    from .models import OrderPaymentChange
    methods = dict(PAYMENT_METHODS)
    labels = (('date', 'Date'), ('amount', 'Amount'), ('method', 'Paid by'), ('card_type', 'Card type'), ('check_number', 'Check #'), ('reference', 'Reference'), ('tendered', 'Amount received'))
    paid_by = lambda s: s.get('card_type') or (f"Check #{s['check_number']}" if s.get('method') == 'check' and s.get('check_number') else methods.get(s['method'], s['method']))
    describe = lambda s: f"{paid_by(s)} ${Decimal(s['amount']):,.2f} dated {s['date']}"
    if action == 'delete':
        summary = f'Deleted payment: {describe(before)}' + (f" ({before['reference']})" if before.get('reference') else '')
    else:
        changes = []
        for key, label in labels:
            if before.get(key) != after.get(key):
                old, new = before.get(key, ''), after.get(key, '')
                if key == 'method': old, new = methods.get(old, old), methods.get(new, new)
                if key == 'amount': old, new = f'${Decimal(old):,.2f}', f'${Decimal(new):,.2f}'
                changes.append(f'{label} {old or "(blank)"} -> {new or "(blank)"}')
        summary = f'Corrected {describe(before)}: ' + '; '.join(changes)
    actor = actor or {}
    OrderPaymentChange.objects.create(order=order, payment=payment, action=action, summary=summary, before=before, after=after,
                                      performed_by=str(actor.get('by') or '')[:150], cashier=str(actor.get('cashier') or '')[:150],
                                      client_address=str(actor.get('address') or '')[:64])


@transaction.atomic
def save_order_payment(order_id,data,actor=None):
    """Record, correct or delete a payment on a customer order. A payment is dated the day
    it is recorded, never another day; a payment from an EARLIER store day keeps its amount, method and date and can't
    be deleted (only its reference changes); a same-day payment is fully editable and re-locks its tax. A reference-only
    edit never touches the tax lock. Corrections and deletes are logged (`actor` = dict(by, cashier, address))."""
    order=Document.objects.select_for_update().get(pk=order_id,kind='order')
    if order.imported: raise WorkflowError('Documents imported from QuickBooks are read-only.')
    if order.status=='cancelled':
        # payments, refunds and corrections stop once an order is cancelled (its deposit was refunded at Cancel).
        raise WorkflowError(f'{order.numbered_label} was cancelled. Payments can no longer be recorded or changed on it.')
    if str(order.version)!=str(data.get('version')): raise WorkflowError('This order changed. Reload before recording payment.')
    unused_before=order.unused_payments
    refunded=False   # a refund recorded (or corrected) now settles an order it leaves paid in full or overpaid
    key=data.get('payment_id')
    payment=order.payments.filter(pk=key).first() if str(key or '').isdigit() else None
    if key and not payment: raise WorkflowError('Payment not found on this order.')
    if payment and payment.adjustment: raise WorkflowError(ADJUSTMENT_MESSAGE)
    earlier=payment is not None and payment.earlier_day
    if data.get('action')=='delete':
        if not payment: raise WorkflowError('Choose a payment.')
        if earlier: raise WorkflowError(EARLIER_DAY_MESSAGE)
        before=payment_snapshot(payment)
        payment.delete()
        log_payment_change(order, None, 'delete', before, {}, actor)
    else:
        try: token=uuid.UUID(str(data.get('token')))
        except (ValueError,TypeError): raise WorkflowError('Reload before recording payment.')
        if not payment and OrderPayment.objects.filter(token=token).exists(): raise WorkflowError('This payment was already recorded. Reload the order.')
        before=payment_snapshot(payment) if payment else None
        if payment is None:
            # (matching QB): a new payment or refund records who took it (active PIN cashier or signed-in admin,
            # from the session via `actor`). A correction keeps the original; the change log records the corrector.
            payment=OrderPayment(order=order,token=token,created_at=timezone.now(),cashier=str((actor or {}).get('cashier') or '')[:100])
        amount=decimal(data.get('amount'),2,'payment',minimum=Decimal('-99999999'))
        if amount==0: raise WorkflowError('Enter a nonzero payment. Use a negative amount for a refund.')
        method=payment_method(data.get('method'))
        new_date=None
        if data.get('payment_date'):
            try: new_date=date.fromisoformat(str(data['payment_date']))
            except (ValueError,TypeError): raise WorkflowError('Enter a valid payment date.')
        money_changed=before is None or (amount,method)!=(money(payment.amount),payment.method)
        date_changed=new_date is not None and new_date!=payment.store_day
        if earlier and (money_changed or date_changed): raise WorkflowError(EARLIER_DAY_MESSAGE)
        if date_changed: raise WorkflowError(DATED_TODAY_MESSAGE)
        payment.amount=amount;payment.method=method;payment.reference=short(data.get('reference',''),200,'Payment reference')
        # card type on a card payment, the sale screen's list. Like the reference it is not money: an earlier-day
        # payment may change it (no re-lock, logged as a correction); it is cleared when the method isn't a card.
        card_type=short(data.get('card_type',''),100,'Card type') if method=='credit' else ''
        if card_type and card_type not in CARD_TYPES: raise WorkflowError('Choose Visa, MasterCard, American Express or Discover.')
        payment.card_type=card_type
        # (matching QB check tenders): a Check payment or refund records its check number. Required on a new check
        # row and when a row becomes a check; a check recorded before (no number) can still save its reference.
        # Not money (no re-lock), so an earlier-day payment may correct it like the reference; cleared for other
        # methods. A form without the box keeps what was saved.
        if method=='check':
            raw=data.get('check_number') if 'check_number' in data else payment.check_number
            check_number=short(str(raw or '').strip().lstrip('#').strip(),20,'Check number')
            if check_number and not CHECK_NUMBER.fullmatch(check_number): raise WorkflowError('Enter the check number with letters, digits or dashes only.')
            old_unnumbered=before is not None and before['method']=='check' and not before.get('check_number')
            if not check_number and not old_unnumbered: raise WorkflowError('Enter the check number.')
            payment.check_number=check_number
        else: payment.check_number=''
        # (matching QB deposit receipts): on a Cash payment, the Amount received (cash handed over) is kept for the
        # order screen and printout; the change is received - payment. Display only: the payment amount, its tax lock
        # and Daily Totals never use it. A form without the box (earlier-day rows) keeps what was saved.
        if 'tendered' in data:
            raw=str(data.get('tendered') or '').strip()
            if method=='cash' and amount>0 and raw:
                tendered=decimal(raw,2,'amount received')
                if tendered<amount: raise WorkflowError(f'The amount received (${tendered:,.2f}) is less than the payment (${amount:,.2f}). Enter the cash handed over, or leave it blank.')
                payment.tendered=tendered
            else: payment.tendered=None
        if method!='cash' or amount<=0 or (payment.tendered is not None and payment.tendered<amount): payment.tendered=None
        if payment.amount>0 and money_changed and str(data.get('confirm_overpayment') or '')!='1':
            # add-on: "give a warning if we're taking an overpayment" — the cashier decides, the app
            # only asks. The page asks before posting; a post without the answer (stale page) is refused with the
            # same words. Balance due as the page shows it: the order total less every payment but this one.
            prior=money(before['amount']) if before else Decimal('0')
            balance=money((order.total or Decimal('0'))-(order.paid-prior))
            if payment.amount>balance: raise WorkflowError(overpayment_message(payment.amount-balance, balance))
        if payment.amount<0: qb_order_guard(order, 'refund')  # review finding 4: no refunds on an unreconciled QB deposit
        if payment.method=='account':
            customer=Customer.objects.select_for_update().get(pk=order.customer_id)
            if not customer.charge_account:
                raise WorkflowError(f'{customer.display_name} is not set up as a charge account. Turn on Charge Account in Edit Customer first.')
            prior=Decimal('0') if not payment.pk else OrderPayment.objects.filter(pk=payment.pk,method='account').values_list('amount',flat=True).first() or Decimal('0')
            new_balance=customer.account_balance-prior+payment.amount
            if customer.account_limit and payment.amount>0 and new_balance>customer.account_limit and data.get('override_limit')!='1':
                raise WorkflowError(f'This charge brings {customer.display_name} to ${new_balance:,.2f}, over the ${customer.account_limit:,.2f} account limit.')
        payment.save()
        if money_changed:
            # the tax inside this payment is fixed now, for the order as it stands today. A
            # reference-only edit leaves the lock exactly as it was (part 4).
            from reports.tax_ledger import lock_payment
            lock_payment(order, payment)
            refunded=payment.amount<0
        if before is not None:
            after=payment_snapshot(payment)
            if after!=before: log_payment_change(order, payment, 'correct', before, after, actor)
    if (order.deposit_used or Decimal('0')) > 0 and order.unused_payments < 0:
        # Review finding 5 (order 972): QuickBooks already used part of the deposit on a pickup there; a refund,
        # correction or delete must not take back money that pickups already consumed.
        raise WorkflowError(f"QuickBooks already used ${order.deposit_used:,.2f} of this order's deposit on a pickup, so this would leave "
                            f'${-order.unused_payments:,.2f} more applied to pickups than paid. Unused payments before this change: ${max(unused_before, Decimal("0")):,.2f}.')
    accounts.refresh(order.customer_id)  # also after a delete
    recalculate_order(order, after_refund=refunded)
    return order


@transaction.atomic
def fulfill_order(order_id,data,cashier=None):
    """Ring a pickup of `order` as a completed sales receipt. `cashier`: the active cashier's name (PIN cashier
    or signed-in admin, as for any receipt; None leaves it blank), shown in Sales History and on the receipt screen."""
    order=Document.objects.select_for_update().get(pk=order_id,kind='order')
    if order.imported: raise WorkflowError('Documents imported from QuickBooks are read-only.')
    if order.status=='cancelled': raise WorkflowError(f'{order.numbered_label} was cancelled. Nothing can be picked up on it.')
    if str(order.version)!=str(data.get('version')): raise WorkflowError('This order changed. Reload before pickup.')
    qb_order_guard(order, 'pickup')  # review findings 2 and 4
    from .discounts import pickup_share
    rows=[];sources=[];exts=[];exacts=[]
    for line in order.lines.select_related('item'):
        raw=data.get(f'qty_{line.pk}','0')
        qty=decimal(raw or '0',0,'pickup quantity')  # whole numbers only
        if qty==0: continue
        remaining=line.quantity-line.fulfilled
        if qty>remaining: raise WorkflowError('Pickup quantity exceeds the remaining quantity.')
        # The line's share of the order discount travels with the pickup: a partial pickup takes the share of
        # the quantity picked up, the last pickup of the line takes whatever is left of it.
        row=dict(item_id=line.item_id,sku=line.sku,item_name=line.item_name,description=line.description,vendor=line.vendor,quantity=str(qty),price=str(line.price),tax_rate=str(line.tax_rate or 0),pickup_line=line.pk,receipt_discount=str(pickup_share(line,qty)))
        exact=qty*line.price;ext=money(exact)
        if qty==remaining and qty!=line.quantity:
            # Last pickup of a line picked up in parts: its extension is what is left of the line's own extension, so a
            # fraction-of-a-cent price (2 x 355.455 = 710.91, not 355.46 + 355.46) still adds up to the order; the receipt
            # keeps it as the recorded line total, the way imported QB lines with odd prices do.
            earlier=sum((delivery.amount for delivery in line.deliveries.filter(document__status='completed')),Decimal('0'))
            if line.amount-earlier!=ext: ext=exact=line.amount-earlier;row['recorded_amount']=str(ext)
        rows.append(row);sources.append(line);exts.append(ext);exacts.append(exact)
    if not rows: raise WorkflowError('Select at least one item and pickup quantity.')
    # Pickup total as save_document will figure it: gross extensions - discount shares + tax on the exact net amounts.
    shares=sum((Decimal(r['receipt_discount']) for r in rows),Decimal('0'))
    tax=tax_amount(((exact-Decimal(r['receipt_discount']),Decimal(r['tax_rate'])) for exact,r in zip(exacts,rows)),order.tax_exempt)
    completed=order.fulfillments.filter(status='completed')
    final=all(any(r['pickup_line']==line.pk and Decimal(r['quantity'])==line.quantity-line.fulfilled for r in rows) for line in order.lines.all() if line.quantity-line.fulfilled>0)
    pickup_tax=None
    if final and order.tax is not None:
        # The last pickup squares the tax with the order (see save_document): the order's tax less earlier pickups'.
        remainder=order.tax-sum(completed.values_list('tax',flat=True),Decimal('0'))
        if remainder!=tax and remainder>=0: pickup_tax=remainder;tax=remainder
    total=sum(exts,Decimal('0'))-shares+tax
    # Review finding 5: what the pickup may use is the order's money not yet applied — the same basis as
    # `applied_payments`, which includes the deposit QuickBooks already used on pickups there (order 972: $200).
    available=order.paid-order.applied_payments
    if total>available:
        used_note=f' (QuickBooks already used ${order.deposit_used:,.2f} of its deposit on a pickup there)' if (order.deposit_used or Decimal('0'))>0 else ''
        raise WorkflowError(f'Record another ${money(total-available)} on this order before this pickup{used_note}.')
    # Parent stores actual tender history. Receipt records use of those payments, not a new collection.
    # `pickup` lets the receipt carry items that are in the Items Trash: only this order's own lines, locked above.
    receipt=save_document(dict(action='complete',version=0,customer_id=order.customer_id,lines=rows,payment_method='other',tendered=str(total),notes=f'Pickup from {order.numbered_label}',shipping_address=order.shipping_address,tax_exempt=order.tax_exempt,
                               discount_percent=str(order.discount_percent or '0'),discount_reason=order.discount_reason,discount_note=order.discount_note,
                               pickup_tax=str(pickup_tax) if pickup_tax is not None else None), 'sale', data.get('token'), cashier=cashier, pickup={line.pk:line for line in sources})
    if receipt.source_order_id: raise WorkflowError('This pickup was already recorded.')
    receipt.source_order=order;receipt.payment_method='order';receipt.save(update_fields=['source_order','payment_method'])
    for row,original in zip(receipt.lines.all(),sources): row.source_order_line=original;row.save(update_fields=['source_order_line'])
    recalculate_order(order)
    return receipt
