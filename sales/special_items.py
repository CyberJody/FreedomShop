# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a special-order item follows its sales order line.

In QB POS the special-order piece is its own inventory item, and the PO is made from the item, so when staff change
the piece on the customer's order (a different finish), the item has to say the same or the vendor gets the old
finish. When a sales order line's name or description is edited on the order and its item is a special-order item
that belongs to this order only ('s test: SOLD in the name, "(SOLD" in the description or the SPECIAL ORDER
department, on one customer's orders, and on no other open or completed order), saving the order updates the item's
name/description to match. Open, not-yet-received PO lines for that item take the new text too, unless the PO line's
description was edited on the PO itself. A PO that has already received the item (or a PO brought over from
QuickBooks) is left alone, with a note to tell the vendor. Stock items are shared, so an edit stays on the order line.
"""
from .models import Item, Line


def _changed_on_order(line, was):
    """True when this save changed the line's name or description (or added the line already edited)."""
    if was is None or was[0] != line.item_id:
        return True
    return (was[1], was[2]) != (line.item_name, line.description)


def sync_special_items(order, lines, old_text):
    """Called by save_document inside its transaction after an order's lines are saved. `old_text` = {line pk (str):
    (item_id, item_name, description)} as the lines were before this save. Returns notes for the screen."""
    from purchasing.item_views import same_name_items
    from purchasing.models import PurchaseLine
    from purchasing.order_coverage import special_item_ids
    notes = []
    candidates = []
    for line in lines:
        if not line.item_id or line.quantity <= 0 or line.returned_from_line_id:
            continue
        was = old_text.get(str(line.pk)) if line.pk else None
        if not _changed_on_order(line, was):
            continue
        item = line.item
        new_name = (line.item_name or '').strip()
        new_description = (line.description or '').strip()
        if (new_name or item.display_name) == item.display_name and (new_description or item.description) == item.description:
            continue
        candidates.append((line, item, new_name, new_description))
    if not candidates:
        return notes
    specials = special_item_ids([item for _, item, _, _ in candidates])
    for line, item, new_name, new_description in candidates:
        if item.pk not in specials:
            continue   # a stock item (or a SOLD piece shared by several customers): the edit stays on the order line
        if Line.objects.filter(item_id=item.pk, document__kind='order').exclude(document=order).exclude(document__status='cancelled').exists():
            continue   # on another order too: not this order's own piece
        item = Item.objects.select_for_update().get(pk=item.pk)
        old_name, old_description = item.display_name, item.description
        fields = []
        if new_name and new_name != item.name:
            item.name = new_name; fields.append('name')
        if new_description and new_description != item.description:
            item.description = new_description; fields.append('description')
        if not fields:
            continue
        item.version += 1
        item.save(update_fields=fields + ['version'])
        notes.append(f'Item #{item.sku} in the Item List was changed to match this order.')
        if 'name' in fields:
            # only a note, and only when another active item really has the same name.
            others = same_name_items(item.name, exclude_pk=item.pk)
            if others:
                notes.append(f"Item{'s' if len(others) > 1 else ''} {', '.join('#' + s for s in others)} "
                             f"{'also use' if len(others) > 1 else 'also uses'} the name {item.name}.")
        updated, received, own = set(), set(), set()
        for po_line in PurchaseLine.objects.select_for_update().filter(item_id=item.pk, document__kind='po').exclude(
                document__status='cancelled').select_related('document').order_by('document_id', 'position'):
            po = po_line.document
            if po.imported or po_line.received > 0 or po_line.imported_received > 0 or po.status == 'closed':
                received.add(po.number)
                continue
            if po_line.description != old_description:
                own.add(po.number)   # edited on the PO itself; the PO's wording stays
                continue
            po_line.description = item.description
            if po_line.item_name in ('', old_name):
                po_line.item_name = item.display_name
            po_line.save(update_fields=['description', 'item_name'])
            if po.number not in updated:
                po.version += 1
                po.save(update_fields=['version'])
            updated.add(po.number)
        for number in sorted(updated):
            notes.append(f'{number} was changed to match (not received yet). If it was already sent to the vendor, send it again.')
        for number in sorted(received):
            notes.append(f'This item is already on {number}; tell the vendor about the change.')
        for number in sorted(own):
            notes.append(f'{number} has its own description for this item and was not changed; check it.')
    return notes
