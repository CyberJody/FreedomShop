# SPDX-License-Identifier: AGPL-3.0-or-later
"""Items Trash: the soft delete behind "Delete Selected" on the Item List, and Restore.

Trash = the existing Item.active flag, made proper:
- no permanent delete; a trashed item stays on every saved document, history list and report;
- stock on hand is zeroed with an inventory adjustment "Moved to Trash" (QB POS writes an adjustment memo when it
  deletes an item with quantity); Restore puts the same quantity back with "Restored from Trash";
- items on open sales orders, held receipts, open POs or furniture-set tags are warned about but still trashed;
  their existing lines keep working (sales.services / purchasing.services only refuse NEW lines);
- a return of a trashed item restocks it and it stays in the Trash;
- Admin Mode only (the views enforce it);
- an ItemTrash row keyed by item number survives `rebuild_from_backup`, which reloads every QB item as active, so
  `reapply` re-trashes them after the load (this also changes the import).
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from purchasing.models import PurchaseLine
from .models import InventoryAdjustment, Item, ItemTrash, Line

TRASH_NOTE = 'Moved to Trash'
RESTORE_NOTE = 'Restored from Trash'
MAX_BATCH = 500


def never_used(queryset):
    """Items with no sales, order, held-receipt, PO/voucher or furniture-set-tag history (the "Never used" filter)."""
    from tags.models import FurnitureSetLine
    return queryset.filter(~Exists(Line.objects.filter(item=OuterRef('pk'))),
                           ~Exists(PurchaseLine.objects.filter(item=OuterRef('pk'))),
                           ~Exists(FurnitureSetLine.objects.filter(item=OuterRef('pk'))))


def usage(items):
    """Per item: the open documents and sets that still point at it, for the Delete / Restore windows.

    Returns {item.pk: dict(on_hand, orders=[Document], held=[Document], pos=[(PurchaseDocument, due)], sets=[FurnitureSet],
    sold=bool)} — the same warnings QB POS 18's "Delete Items" window shows (quantity on hand, due on a customer order,
    due on a purchase order), plus held receipts and furniture-set tags, which the app also keeps live links to.
    """
    from tags.models import FurnitureSetLine
    pks = [i.pk for i in items]
    rows = {pk: dict(orders=[], held=[], pos=[], sets=[], sold=False) for pk in pks}
    seen = defaultdict(set)
    for line in Line.objects.filter(item_id__in=pks, document__kind='order', document__status='open').select_related('document').order_by('document__created_at'):
        if line.document_id not in seen[('order', line.item_id)]:
            seen[('order', line.item_id)].add(line.document_id)
            rows[line.item_id]['orders'].append(line.document)
    for line in Line.objects.filter(item_id__in=pks, document__kind='sale', document__status='held').select_related('document').order_by('document__created_at'):
        if line.document_id not in seen[('held', line.item_id)]:
            seen[('held', line.item_id)].add(line.document_id)
            rows[line.item_id]['held'].append(line.document)
    for line in PurchaseLine.objects.filter(item_id__in=pks, document__kind='po', document__status='open').select_related('document').order_by('document__created_at'):
        if line.due > 0 and line.document_id not in seen[('po', line.item_id)]:
            seen[('po', line.item_id)].add(line.document_id)
            rows[line.item_id]['pos'].append((line.document, line.due))
    for line in FurnitureSetLine.objects.filter(item_id__in=pks).select_related('tag_set').order_by('tag_set__name'):
        if line.tag_set_id not in seen[('set', line.item_id)]:
            seen[('set', line.item_id)].add(line.tag_set_id)
            rows[line.item_id]['sets'].append(line.tag_set)
    for pk in Line.objects.filter(item_id__in=pks, document__kind='sale', document__status='completed').values_list('item_id', flat=True).distinct():
        rows[pk]['sold'] = True
    for item in items:
        rows[item.pk]['on_hand'] = item.on_hand
        rows[item.pk]['warnings'] = bool((item.on_hand or 0) != 0 or rows[item.pk]['orders'] or rows[item.pk]['held'] or rows[item.pk]['pos'] or rows[item.pk]['sets'])
    return rows


def _zero_stock(item, note):
    """Zero the item's on-hand quantity with an adjustment; returns (quantity, value) that was on hand, or (None, None)
    if there was nothing to do. The value is quantity × average cost in whole cents (None when the cost is unknown)."""
    if item.on_hand is None or item.on_hand == 0:
        return None, None
    before = item.on_hand
    InventoryAdjustment.objects.create(item=item, old_quantity=before, new_quantity=Decimal('0'), old_cost=item.average_cost, new_cost=item.average_cost, note=note)
    item.on_hand = Decimal('0')
    return before, stock_value(before, item.average_cost)


def stock_value(quantity, cost):
    """Inventory value in whole cents (half-up, as QB POS), or None when the average cost is unknown."""
    if quantity is None or cost is None:
        return None
    return (quantity * cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def parked(item):
    """The quantity set aside for an item in the Trash (its `quantity_zeroed`), 0 for anything else.

    Receiving while an item is trashed (an existing PO line, decision 3) and Restore both work "as if the item had never
    left": the parked units keep counting in the weighted average cost, so purchasing.services adds this to the live
    on-hand quantity when it reconciles a voucher post, edit or cancel. The item's average cost therefore already holds
    the blended basis of live + parked units, and Restore only moves the parked quantity back.
    """
    if item.active or item.on_hand is None:
        return Decimal('0')
    quantity = ItemTrash.objects.filter(Q(item=item) | Q(sku=item.sku)).values_list('quantity_zeroed', flat=True).first()
    return quantity or Decimal('0')


@transaction.atomic
def trash(item_pks, by=''):
    """Move the given active items to the Items Trash. Returns the ItemTrash rows written (skips items already there)."""
    pks = sorted({int(pk) for pk in item_pks})
    if not pks or len(pks) > MAX_BATCH:
        raise ValueError(f'Choose between 1 and {MAX_BATCH} items.')
    now = timezone.now()
    entries = []
    for item in Item.objects.select_for_update().filter(pk__in=pks, active=True).order_by('pk'):
        zeroed, value = _zero_stock(item, TRASH_NOTE)
        item.active = False
        item.version += 1
        item.save(update_fields=['on_hand', 'active', 'version'])
        entry, _ = ItemTrash.objects.update_or_create(sku=item.sku, defaults=dict(item=item, item_name=item.display_name[:200], trashed_at=now, trashed_by=by[:100], quantity_zeroed=zeroed, value_zeroed=value))
        entries.append(entry)
    return entries


@transaction.atomic
def restore(item_pks, by=''):
    """Bring items back from the Trash: active again, the zeroed stock put back with a "Restored from Trash" adjustment.

    The average cost is left as it stands: while the item was trashed, receiving reconciled it against the parked
    quantity too (see `parked`), so it already equals the never-trashed cost."""
    pks = sorted({int(pk) for pk in item_pks})
    if not pks or len(pks) > MAX_BATCH:
        raise ValueError(f'Choose between 1 and {MAX_BATCH} items.')
    restored = []
    for item in Item.objects.select_for_update().filter(pk__in=pks, active=False).order_by('pk'):
        entry = ItemTrash.objects.filter(Q(item=item) | Q(sku=item.sku)).first()
        quantity = entry.quantity_zeroed if entry else None
        if quantity:
            before = item.on_hand if item.on_hand is not None else Decimal('0')
            # A return of a trashed item may have restocked it meanwhile (decision 6): add the zeroed quantity back on top.
            InventoryAdjustment.objects.create(item=item, old_quantity=before, new_quantity=before + quantity, old_cost=item.average_cost, new_cost=item.average_cost, note=RESTORE_NOTE)
            item.on_hand = before + quantity
        item.active = True
        item.version += 1
        item.save(update_fields=['on_hand', 'active', 'version'])
        if entry:
            entry.delete()
        restored.append(item)
    return restored


def reapply(say=None):
    """After a rebuild from a QB backup (every item reloaded as active): re-trash the recorded item numbers.

    Zeroes the reloaded QB stock with the same "Moved to Trash" adjustment, keeps who/when from the original trash
    entry, and records the newly zeroed quantity so Restore puts back what the backup held. Returns counts.
    """
    say = say or (lambda message: None)
    entries = {e.sku: e for e in ItemTrash.objects.all()}
    retrashed, missing = 0, 0
    for item in Item.objects.filter(sku__in=list(entries)).order_by('pk'):
        entry = entries.pop(item.sku)
        zeroed, value = _zero_stock(item, TRASH_NOTE)
        item.active = False
        item.version += 1
        item.save(update_fields=['on_hand', 'active', 'version'])
        entry.item = item
        entry.item_name = item.display_name[:200]
        entry.quantity_zeroed = zeroed
        entry.value_zeroed = value
        entry.save(update_fields=['item', 'item_name', 'quantity_zeroed', 'value_zeroed'])
        retrashed += 1
    for entry in entries.values():
        # The item number is not in this backup (QB deleted it): keep the record; it applies if a later backup has it.
        entry.item = None
        entry.save(update_fields=['item'])
        missing += 1
    say(f'items trash: re-trashed {retrashed}, {missing} trashed item number(s) not in this backup')
    return dict(retrashed_items=retrashed, trashed_skus_not_in_backup=missing)
