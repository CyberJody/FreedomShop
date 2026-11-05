# SPDX-License-Identifier: AGPL-3.0-or-later
"""Purchase edits reconcile receiving effects and retain linked line identities."""
from config.provenance import native_source
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from sales.models import Item, Document as SalesDocument, Line as SalesLine
from sales.services import WorkflowError
from sales import trash
from .procurement import procurement_capacity
from .models import PurchaseDocument, PurchaseLine, ReceivingMovement, Vendor


@transaction.atomic
def save_purchase(header, rows, kind, action, source_po=None, source_order=None):
    if action not in ({'save','cancel'} if kind=='po' else {'hold','post','cancel'}):
        raise WorkflowError('Choose an available action.')
    rows=[r for r in rows if r and not r.get('DELETE') and r.get('quantity',0)>0]
    sales_ids=[r['sales_line'] for r in rows if r.get('sales_line')]
    sales_lines={l.pk:l for l in SalesLine.objects.filter(pk__in=sales_ids).select_related('document')}
    list(SalesDocument.objects.select_for_update().filter(pk__in={l.document_id for l in sales_lines.values()}).order_by('pk'))
    sales_lines={l.pk:l for l in SalesLine.objects.filter(pk__in=sales_ids).select_related('document')}
    po=PurchaseDocument.objects.select_for_update().get(pk=source_po.pk) if source_po else None
    doc=PurchaseDocument.objects.select_for_update().filter(token=header['token']).first()
    if doc and (doc.kind!=kind or doc.source_po_id!=(po.pk if po else None)):
        raise WorkflowError('Document type or source does not match.')
    if doc and doc.imported: raise WorkflowError('Documents imported from QuickBooks are read-only.')
    if doc and kind=='po' and doc.status=='cancelled':
        # cancelling a PO is final, like a cancelled receipt. A save used to re-open it silently.
        raise WorkflowError(f'{doc.number} was cancelled. It can no longer be changed or received.')
    if doc and kind=='voucher' and doc.status=='cancelled':
        # a cancelled (reversed) voucher is final. A save used to receive it all over again.
        raise WorkflowError(f'{doc.number} was cancelled. It can no longer be changed or received again.')
    if doc and kind=='voucher' and doc.status=='posted' and action!='cancel':
        # (matching QB): a received voucher isn't put back on hold or re-saved (Put on Hold used to take its items
        # back out of stock without a word). A receiving mistake is fixed with Reverse Voucher, then a new receiving.
        raise WorkflowError(f'{doc.number} was already received. To fix a receiving mistake, use Reverse Voucher, then receive the items again.')
    if doc and doc.version!=header['version']:
        raise WorkflowError('Someone changed this document. Reload before saving.')
    if action=='cancel' and not doc: raise WorkflowError('Save the document first.')
    old_lines={l.pk:l for l in doc.lines.all()} if doc else {}
    old_movements={m.item_id:m for m in doc.receivingmovement_set.all()} if doc else {}
    if action!='cancel':
        if not 1<=len(rows)<=100: raise WorkflowError('Add between 1 and 100 items with quantities greater than zero.')
        vendor=Vendor.objects.get(pk=header['vendor'].pk)
        if not vendor.active: raise WorkflowError('Select an active vendor.')
        if po and (kind!='voucher' or po.kind!='po' or po.status=='cancelled' or po.vendor_id!=vendor.pk):
            if po.status=='cancelled': raise WorkflowError(f'{po.number} was cancelled. Nothing can be received on it.')
            raise WorkflowError('Receiving requires a purchase order for the selected vendor.')
    else:
        vendor=doc.vendor; rows=[]
    items={i.pk:i for i in Item.objects.select_for_update().filter(pk__in=set(old_movements)|{r['item'].pk for r in rows}).order_by('pk')}
    po_lines={l.pk:l for l in po.lines.all()} if po else {}
    seen_ids,seen_po,seen_so=set(),set(),set()
    quantities=defaultdict(Decimal);values=defaultdict(Decimal);new_lines=[]
    for position,row in enumerate(rows):
        item=items[row['item'].pk]
        key=row.get('line_id')
        line=old_lines.get(key) if key else None
        if key and (not line or key in seen_ids): raise WorkflowError('Reload this document before editing its lines.')
        if key: seen_ids.add(key)
        if not item.active:
            # Items Trash: a trashed item stays on lines it is already on (re-saving this PO or
            # voucher, receiving a PO line that holds it) but can't be put on a new line.
            from_po=po_lines.get(row.get('source_line')) if po else None
            if not ((line and line.item_id==item.pk) or (from_po and from_po.item_id==item.pk)):
                raise WorkflowError(f'{item.display_name} is in the Items Trash. Restore it from the Items Trash (admin sign-in) before ordering it again.')
        if item.unorderable: raise WorkflowError(f'{item.display_name} is marked Unorderable. Clear that setting to purchase it.')
        line=line or PurchaseLine()
        line.position=position;line.item=item;line.sku=item.sku;line.item_name=item.display_name;line.description=item.description
        if kind=='po':
            # the PO line keeps its own (editable) description; blank falls back to the item's. Never written
            # back to the Item or the customer order.
            line.description=(row.get('item_description') or '').strip()[:1000] or item.description
        line.quantity=row['quantity'];line.cost=row['cost'];line.recorded_amount=None
        if line.received and line.quantity<line.received: raise WorkflowError('Ordered quantity cannot be below received quantity. Edit the receiving voucher first.')
        if po:
            original=po_lines.get(row.get('source_line'))
            if not original or original.item_id!=item.pk or original.pk in seen_po:
                raise WorkflowError('Each receiving line must match a unique item line on this PO.')
            seen_po.add(original.pk)
            already=original.imported_received+(PurchaseLine.objects.filter(native_source('document__source_id'),source_line=original,document__status='posted').exclude(document=doc).aggregate(q=Sum('quantity'))['q'] or Decimal('0'))
            if line.quantity>original.quantity-already: raise WorkflowError(f'{item.sku}: receiving quantity exceeds the quantity remaining on this PO.')
            line.source_line=original
            line.description=original.description or item.description  # the voucher carries the PO line's text
        elif row.get('source_line'): raise WorkflowError('A purchase order reference is required.')
        if row.get('sales_line'):
            original=sales_lines.get(row['sales_line'])
            if kind!='po' or not original or original.document.kind!='order' or original.document.status=='cancelled' or original.pk in seen_so or (original.item_id and original.item_id!=item.pk):
                raise WorkflowError('Select a matching item from an active customer order.')
            seen_so.add(original.pk)
            if kind=='po' and line.sales_line_id!=original.pk and original.item_id:
                # a customer-order line goes on a PO to its item's vendor (primary, or one of its alternate vendors).
                source_item=original.item
                if source_item.primary_vendor_id and source_item.primary_vendor_id!=vendor.pk and not source_item.alternate_vendors.filter(vendor=vendor).exists():
                    raise WorkflowError(f'{source_item.display_name} is ordered from {source_item.primary_vendor.company}, not {vendor.company}. Skip that line here and make a separate PO for {source_item.primary_vendor.company}.')
            already=PurchaseLine.objects.filter(sales_line=original,document__kind='po').exclude(document__status='cancelled').exclude(document=doc).aggregate(q=Sum('quantity'))['q'] or Decimal('0')
            if line.quantity > procurement_capacity(original, doc):
                raise WorkflowError(f'{original.description}: quantity is already covered by another purchase order or pickup.')
            line.sales_line=original
        quantities[item.pk]+=line.quantity;values[item.pk]+=line.amount;new_lines.append(line)
    if doc and action!='cancel':
        # a PO line whose item labels were printed keeps its piece codes for good, so it can't be removed.
        from .models import PieceLabel
        labelled=PieceLabel.objects.filter(line__in=[pk for pk in old_lines if pk not in seen_ids]).select_related('line').first()
        if labelled:
            name=labelled.line.item_name or labelled.line.sku
            raise WorkflowError(f'{name} already has item labels printed for this PO, so it can\'t be taken off. Keep the line (lower its quantity if fewer are coming), or use Cancel Remaining PO.')
    total=sum(values.values(),Decimal('0.00'))
    if total>Decimal('9999999999.99'): raise WorkflowError('The document total is too large.')
    # Reverse the previous effect and apply the revised effect exactly once.
    new_quantities=quantities if action=='post' else {}
    for pk in set(old_movements)|set(new_quantities):
        item=items[pk];old=old_movements.get(pk)
        delta=new_quantities.get(pk,Decimal('0'))-(old.quantity if old else Decimal('0'))
        value_delta=(values[pk] if action=='post' else Decimal('0'))-(old.value if old else Decimal('0'))
        if not delta and not value_delta: continue
        # Items Trash: an item received while in the Trash still owns the stock that was parked when it was trashed,
        # so its average cost is reconciled over live + parked units, as if it had never left (Restore moves them back).
        held=item.on_hand+trash.parked(item) if item.on_hand is not None else None
        if held is None:
            # only an imported QB item can still have an unknown count; app-made items start at 0.
            raise WorkflowError(f'Item {item.sku} has no stock count yet. Ask a manager to set its On-Hand Qty in the Item List.')
        if held>0 and item.average_cost is None:
            raise WorkflowError(f'Item {item.sku} has stock but no average cost yet. Ask a manager to set its Avg. Unit Cost in the Item List.')
        stock=held+delta
        if abs(item.on_hand+delta)>Decimal('999999999.999'): raise WorkflowError(f'{item.sku}: this edit would make stock invalid. Correct its stock quantity first.')
        stock_value=held*(item.average_cost or Decimal('0'))+value_delta
        if held <= 0 or stock <= 0:
            # A negative count is a quantity discrepancy, not negative inventory value.
            if delta > 0: item.average_cost=(value_delta/delta).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        else:
            if stock_value<0: raise WorkflowError(f'{item.sku}: this edit would make stock value negative. Correct its average cost first.')
            item.average_cost=(stock_value/stock).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        item.on_hand=item.on_hand+delta
        if new_quantities.get(pk): item.order_cost=(values[pk]/new_quantities[pk]).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        item.version+=1;item.save(update_fields=['on_hand','average_cost','order_cost','version'])
    if not doc: doc=PurchaseDocument(token=header['token'],kind=kind,source_po=po,source_order=source_order)
    else: doc.version+=1
    if action=='cancel':
        doc.status='cancelled'
    else:
        doc.status=(header.get('status') or 'open') if kind=='po' else ('posted' if action=='post' else 'held')
        if kind=='po' and doc.status not in {'open','pending','closed'}: raise WorkflowError('Select an available order status.')
        doc.vendor=vendor;doc.vendor_name=vendor.company;doc.total=total
        for field in ['notes','terms','ship_date','alternate_pickup_date','payment_due','invoice_number']: setattr(doc,field,header.get(field))
        doc.posted_at=(doc.posted_at or timezone.now()) if action=='post' else None
    doc.save()
    if action!='cancel':
        mapped_orders=set()
        for line in new_lines:
            if line.sales_line_id and not line.sales_line.item_id:
                original=line.sales_line;original.item=line.item;original.save(update_fields=['item'])
                mapped_orders.add(original.document_id)
        for order in SalesDocument.objects.filter(pk__in=mapped_orders):
            order.version+=1;order.save(update_fields=['version'])
        for line in new_lines: line.document=doc;line.save()
        doc.lines.exclude(pk__in=[l.pk for l in new_lines]).delete()
    ReceivingMovement.objects.filter(document=doc).delete()
    if action=='post':
        for pk,qty in quantities.items(): ReceivingMovement.objects.create(document=doc,item_id=pk,quantity=qty,value=values[pk])
    if po:
        for original in po.lines.all():
            # Quantities QuickBooks had already received on an imported PO stay counted.
            original.received=min(original.quantity,original.imported_received+(original.receiving_lines.filter(native_source('document__source_id'),document__status='posted').aggregate(q=Sum('quantity'))['q'] or Decimal('0')))
            original.save(update_fields=['received'])
        if po.status!='cancelled':  # a cancelled PO stays cancelled when one of its earlier vouchers is cancelled
            po.status='closed' if po.lines.exists() and all(l.due<=0 for l in po.lines.all()) else 'open'
        po.version+=1;po.save(update_fields=['status','version'])
    if kind=='voucher' and action=='cancel':
        # a reversed voucher's pieces come off their truck, so their labels can be scanned on a later truck.
        from .truck import voucher_reversed
        voucher_reversed(doc)
    return doc
