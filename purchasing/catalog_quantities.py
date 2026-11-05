# SPDX-License-Identifier: AGPL-3.0-or-later
from config.provenance import native_source
from datetime import date, datetime, time
from decimal import Decimal
from django.db.models import OuterRef, Subquery, Sum, F, Value, DecimalField, DateField
from django.db.models.functions import Coalesce, Greatest, TruncDate
from django.utils import timezone
from .models import PurchaseLine

# Every on-order figure (Item List, Item Detail, Edit Item, item picker, Reorder Reminders / Create PO)
# counts only open purchase-order lines on POs dated on/after this day, in the shop's time zone.
# Older open POs are not changed: they stay on the PO List, and an item's PO lists still show them greyed as
# "before 2026 - not counted". QB's own on-order figure (Item.imported_on_order) stays in the database but is not read.
ON_ORDER_FROM = date(2026, 1, 1)


def on_order_start():
    """Midnight of ON_ORDER_FROM in a shop's time zone (a PO's date is PurchaseDocument.created_at)."""
    return timezone.make_aware(datetime.combine(ON_ORDER_FROM, time.min), timezone.get_default_timezone())


def counts_on_order(document):
    """True when this PO's lines count toward on order (dated on/after ON_ORDER_FROM)."""
    return document.created_at >= on_order_start()


def open_po_lines():
    """Open PO lines linked to an item with quantity still to receive, imported from QB or made in this app. Imported
    lines carry QB's received quantity in `received`, so quantity - received is what QB still expected."""
    return PurchaseLine.objects.filter(item__isnull=False, document__kind='po', document__status='open',
                                       quantity__gt=F('received'))


def counted_po_lines():
    """The PO lines every on-order figure is built from."""
    return open_po_lines().filter(document__created_at__gte=on_order_start())


def on_order_subquery(ref='pk'):
    """On order for the item at OuterRef(ref): sum of (quantity - received) over counted_po_lines, 0 when none."""
    total = (counted_po_lines().filter(item_id=OuterRef(ref)).order_by().values('item_id')
             .annotate(n=Sum(F('quantity') - F('received'))).values('n'))
    return Coalesce(Subquery(total, output_field=DecimalField()), Value(Decimal('0')), output_field=DecimalField())


def on_order_for(item):
    """On order for one item; the same rule as the catalog_quantities annotation."""
    return counted_po_lines().filter(item=item).aggregate(n=Sum(F('quantity') - F('received')))['n'] or Decimal('0')


def catalog_quantities(qs):
    """Annotate on_order (the rule above) and last_received (the QB value loaded on the item or the newest
    receiving voucher made in this app, whichever is later)."""
    received = PurchaseLine.objects.filter(native_source('document__source_id'), item_id=OuterRef('pk'), document__kind='voucher', document__status='posted', quantity__gt=0).order_by('-document__posted_at').values('document__posted_at')[:1]
    qs = qs.annotate(native_last_received=TruncDate(Subquery(received)))
    return qs.annotate(
        on_order=on_order_subquery(),
        last_received=Greatest(Coalesce('native_last_received', 'imported_last_received'), Coalesce('imported_last_received', 'native_last_received'), output_field=DateField()))
