# SPDX-License-Identifier: AGPL-3.0-or-later
"""Truck Receiving: scan each piece off the truck, then receive the truck in one go.

A truck holds pieces for many POs and vendors. The receiving voucher stays what it is (one vendor, one PO, matching
QB), so Receive Items posts one ordinary voucher per PO on the truck through ``save_purchase``, exactly what the
manual Receive Items screen produces. Until then the truck is only a list of scans: stock and the POs don't change.

Everything is decided on the server; two laptops scanning the same truck both land (one live scan per label).
"""
import uuid
from collections import OrderedDict
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from sales.models import money, quantity_text
from sales.services import WorkflowError
from .models import PieceLabel, PurchaseDocument, TruckScan, TruckSession
from .piece_codes import parse_piece_code
from .piece_labels import whole
from .services import save_purchase

REASON_MAX = 200


def _bump(session):
    session.version += 1
    session.save(update_fields=['version'])


def _line_name(line):
    return line.item_name or line.sku or line.description


def _where(scan):
    """'TRUCK-12' or 'TRUCK-12, voucher RV-000123': where a piece already went."""
    text = scan.session.number
    if scan.voucher_id:
        text += f', voucher {scan.voucher.number}'
    return text


def scan(session, text, user=None, by_name=''):
    """Record one scanned piece. Returns a dict with kind ok/repeat/error and what the screen shows."""
    code = parse_piece_code(text)
    if not code:
        return dict(kind='error', message='Not a piece code', detail=(text or '').strip()[:60])
    with transaction.atomic():
        session = TruckSession.objects.select_for_update().get(pk=session.pk)
        if session.status != 'open':
            return dict(kind='error', message=f'{session.number} was already {session.get_status_display().lower()}. Start a new truck to scan more.', code=code)
        label = PieceLabel.objects.select_related('line__document', 'line__item').filter(code=code).first()
        if label is None:
            return dict(kind='error', message='No label with this code', code=code)
        line, po = label.line, label.line.document
        base = dict(code=code, **piece_panel(label))
        live = label.truck_scans.filter(removed=False).select_related('session', 'voucher').first()
        if live and live.session_id == session.pk:
            return dict(base, kind='repeat', message='Already scanned on this truck')
        if live:
            if live.session.status == 'received':
                return dict(base, kind='error', message=f'Already received on {_where(live)}')
            return dict(base, kind='error', message=f'Already scanned on {_where(live)}, which is still being unloaded')
        if po.kind != 'po' or po.status == 'cancelled':
            return dict(base, kind='error', message=f'{po.number} was cancelled. Nothing can be received on it.')
        if line.item_id is None:
            return dict(base, kind='error', message=f'This line on {po.number} has no inventory item. Receive it on a Receiving Voucher.')
        if line.due <= 0:
            return dict(base, kind='error', message=f'{_line_name(line)} on {po.number} is already fully received')
        on_truck = session.scans.filter(removed=False, label__line=line).count()
        if on_truck >= line.due:
            return dict(base, kind='error', message=f'All {whole(line.due)} still due of {_line_name(line)} on {po.number} are already scanned on this truck')
        try:
            with transaction.atomic():
                TruckScan.objects.create(session=session, label=label, scanned_by=user if user and user.is_authenticated else None,
                                         scanned_by_name=by_name[:100])
        except IntegrityError:
            # The other laptop scanned the same piece a moment earlier.
            live = label.truck_scans.filter(removed=False).select_related('session', 'voucher').first()
            if live and live.session_id == session.pk:
                return dict(base, kind='repeat', message='Already scanned on this truck')
            return dict(base, kind='error', message=f'Already scanned on {_where(live) if live else "another truck"}')
        _bump(session)
        base['on_truck'] = on_truck + 1
        return dict(base, kind='ok', message='Added')


def on_hand_cell(line):
    """the item's current quantity on hand, the Item List's figure, so a stock item at zero is seen
    while unloading and the floor display gets restocked. Stock BEFORE this truck (Receive Items changes it afterwards).
    A PO line with no item (QB-era special order) shows nothing."""
    item = line.item if line.item_id else None
    if item is None or item.on_hand is None:
        return dict(on_hand=None, on_hand_text='', on_hand_low=False)
    return dict(on_hand=item.on_hand, on_hand_text=quantity_text(item.on_hand), on_hand_low=item.on_hand <= 0)


def piece_panel(label):
    line, po = label.line, label.line.document
    return dict(item=_line_name(line), description=line.description, po=po.number, vendor=po.vendor_name,
                piece=label.piece, pieces=whole(line.quantity), line=line.pk, **on_hand_cell(line))


@transaction.atomic
def remove(session, reason, scan_id=None, line_id=None):
    """Take a piece (or every piece of one PO line) off the truck. The PO doesn't change; the label is freed."""
    session = TruckSession.objects.select_for_update().get(pk=session.pk)
    if session.status != 'open':
        raise WorkflowError(f'{session.number} was already {session.get_status_display().lower()}. Nothing can be removed from it.')
    reason = ' '.join((reason or '').split())[:REASON_MAX]
    if not reason:
        raise WorkflowError('Type a reason (for example: damaged, going back).')
    scans = session.scans.filter(removed=False)
    if scan_id is not None:
        scans = scans.filter(pk=scan_id)
    elif line_id is not None:
        scans = scans.filter(label__line_id=line_id)
    else:
        raise WorkflowError('Choose a piece to remove.')
    count = scans.update(removed=True, removed_reason=reason, removed_at=timezone.now())
    if not count:
        raise WorkflowError('That piece is no longer on this truck. Reload the page.')
    _bump(session)
    return count


def rows_for(scans):
    """{PO: [(PO line, quantity)]} for the given live scans, PO and line order kept."""
    groups = OrderedDict()
    for scan in scans:
        line = scan.label.line
        groups.setdefault(line.document, OrderedDict()).setdefault(line, 0)
        groups[line.document][line] += 1
    return groups


def summary(session):
    scans = list(session.scans.filter(removed=False).select_related('label__line__document', 'label__line__item'))
    pos = {s.label.line.document_id for s in scans}
    vendors = {s.label.line.document.vendor_id for s in scans}
    return dict(pieces=len(scans), pos=len(pos), vendors=len(vendors))


@transaction.atomic
def receive(session, user=None):
    """Receive Items: one posted receiving voucher per PO on the truck, all or nothing."""
    session = TruckSession.objects.select_for_update().get(pk=session.pk)
    if session.status != 'open':
        raise WorkflowError(f'{session.number} was already {session.get_status_display().lower()}.')
    scans = list(session.scans.filter(removed=False).select_related('label__line__document__vendor', 'label__line__item')
                 .order_by('label__line__document_id', 'label__line__position', 'label__line_id', 'label__piece'))
    if not scans:
        raise WorkflowError('Scan at least one piece before receiving the truck.')
    vouchers = []
    for po, lines in rows_for(scans).items():
        rows = []
        for line, count in lines.items():
            # Same row the Receive Items screen pre-fills from a PO line (views.row_initial(line, 'po')), with the
            # quantity = pieces scanned instead of everything still due.
            rows.append(dict(item=line.item, item_name=line.item_name or line.sku, item_description=line.description,
                             quantity=Decimal(count), cost=money(line.cost), source_line=line.pk, sales_line=None))
        notes = f'Received by truck: {session.number}' + (f'\n{po.notes}' if po.notes else '')
        header = dict(token=uuid.uuid4(), version=0, vendor=po.vendor, notes=notes[:5000], terms=po.terms, ship_date=None,
                      payment_due=None, invoice_number='')
        if po.vendor is None:
            raise WorkflowError(f'{po.number} has no vendor. Nothing on this truck was received.')
        try:
            voucher = save_purchase(header, rows, 'voucher', 'post', source_po=po)
        except WorkflowError as exc:
            raise WorkflowError(f'{po.number} ({po.vendor_name}): {exc} Nothing on this truck was received.')
        TruckScan.objects.filter(pk__in=[s.pk for s in scans if s.label.line.document_id == po.pk]).update(voucher=voucher)
        vouchers.append(voucher)
    session.status = 'received'
    session.received_at = timezone.now()
    session.version += 1
    session.save(update_fields=['status', 'received_at', 'version'])
    return vouchers


@transaction.atomic
def cancel(session):
    """Stop unloading this truck without receiving anything: every piece is freed for another truck."""
    session = TruckSession.objects.select_for_update().get(pk=session.pk)
    if session.status != 'open':
        raise WorkflowError(f'{session.number} was already {session.get_status_display().lower()}.')
    session.scans.filter(removed=False).update(removed=True, removed_reason='truck cancelled', removed_at=timezone.now())
    session.status = 'cancelled'
    session.version += 1
    session.save(update_fields=['status', 'version'])


def voucher_reversed(voucher):
    """Reverse Voucher hook: a reversed voucher's pieces leave their truck, so they can be scanned again later."""
    sessions = set(TruckScan.objects.filter(voucher=voucher, removed=False).values_list('session_id', flat=True))
    count = TruckScan.objects.filter(voucher=voucher, removed=False).update(removed=True, removed_reason='voucher reversed',
                                                                            removed_at=timezone.now())
    TruckSession.objects.filter(pk__in=sessions).update(version=F('version') + 1)
    return count


def table(session):
    """Truck table, grouped vendor -> PO -> line, plus the removed pieces."""
    scans = list(session.scans.select_related('label__line__document', 'label__line__item', 'voucher')
                 .order_by('label__line__document__vendor_name', 'label__line__document_id', 'label__line__position',
                           'label__line_id', 'label__piece', 'pk'))
    vendors = OrderedDict()
    removed = []
    for s in scans:
        if s.removed:
            removed.append(s)
            continue
        line = s.label.line
        po = line.document
        vendor = vendors.setdefault(po.vendor_name or '(no vendor)', OrderedDict())
        group = vendor.setdefault(po, OrderedDict())
        entry = group.setdefault(line, dict(line=line, name=_line_name(line), ordered=whole(line.quantity),
                                            received=whole(line.received), scans=[], **on_hand_cell(line)))
        entry['scans'].append(s)
    out = []
    for name, pos in vendors.items():
        out.append(dict(vendor=name, pos=[dict(po=po, lines=list(lines.values()),
                                               pieces=sum(len(l['scans']) for l in lines.values()))
                                          for po, lines in pos.items()]))
    return out, removed


def truck_list():
    counts = dict(pieces=Count('scans', filter=Q(scans__removed=False)),
                  pos=Count('scans__label__line__document', filter=Q(scans__removed=False), distinct=True))
    open_trucks = list(TruckSession.objects.filter(status='open').annotate(**counts).order_by('-created_at'))
    received = list(TruckSession.objects.filter(status='received').annotate(**counts).order_by('-received_at')[:30])
    return open_trucks, received


def po_trucks(po):
    """'Received by truck' line on the PO page: the trucks that hold (or received) pieces of this PO."""
    return list(TruckSession.objects.filter(scans__label__line__document=po, scans__removed=False)
                .exclude(status='cancelled').distinct().order_by('created_at'))
