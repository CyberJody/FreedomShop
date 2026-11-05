# SPDX-License-Identifier: AGPL-3.0-or-later
"""One print path for every printable document: choose a printer or Create PDF, remembered per session.

Sources: sale (completed/cancelled sales receipt, native or imported from the QB POS backup) and order (sales order).
Receipts print on Star receipt printers or on Letter paper; sales orders print on Letter. Create PDF renders the same
pages as the printers.
"""
import io
import uuid

from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import Document, Printer
from .printing import PrintError, receipt_job, render_receipt, send
from shop_settings.config import business

PDF = 'pdf'
SESSION_KEY = 'print_targets'


def load(source, token):
    """Return (document, kind, title) for a printable source; kind is 'receipt' or 'order'."""
    try:
        token = uuid.UUID(str(token))
    except ValueError:
        raise Http404
    if source == 'sale':
        doc = get_object_or_404(Document.objects.select_related('customer', 'source_order'), token=token, kind='sale')
        if doc.status not in {'completed', 'cancelled', 'held'}:
            raise PrintError('Complete the receipt before printing it.')
        # QB POS reprints held receipts (Held Receipts > Reprint); the printout is stamped HELD.
        return doc, 'receipt', 'Held Receipt' if doc.status == 'held' else f'Sales Receipt {doc.number}'
    if source == 'order':
        doc = get_object_or_404(Document.objects.select_related('customer'), token=token, kind='order')
        return doc, 'order', f'Sales Order {doc.number}'
    raise Http404


def targets(kind):
    """Printers suited to the document, in display groups. Sales orders do not fit a 72mm receipt roll."""
    printers = Printer.objects.filter(active=True)
    groups = []
    if kind == 'receipt':
        groups.append(('Receipt printers', list(printers.filter(kind='receipt'))))
    groups.append(('Letter printers', list(printers.filter(kind='letter'))))
    return [(label, items) for label, items in groups if items]


def remembered(request, kind):
    session = getattr(request, 'session', None)
    return session.get(SESSION_KEY, {}).get(kind, '') if session is not None else ''


def remember(request, kind, target):
    if getattr(request, 'session', None) is None:
        return
    choices = dict(request.session.get(SESSION_KEY, {}))
    if choices.get(kind) != target:
        choices[kind] = target
        request.session[SESSION_KEY] = choices


def resolve(kind, target):
    """Validate a posted target: 'pdf' or the id of an active printer suited to this document."""
    if target == PDF:
        return PDF
    if str(target).isdigit() and len(str(target)) <= 18:
        for _, printers in targets(kind):
            for printer in printers:
                if str(printer.pk) == str(target):
                    return printer
    raise PrintError('Choose a printer or Create PDF.')


def pages(doc, kind, original=False, letter=False):
    """Images for a document. Receipts are 72mm strips; on Letter they are placed on 300 dpi pages."""
    if kind == 'order':
        from .letter_printing import render_sales_order
        return render_sales_order(doc), 300
    receipt = render_receipt(doc, reprint=not original)
    if letter:
        from .letter_printing import receipt_on_letter
        return receipt_on_letter(receipt), 300
    return [receipt], 203


def print_to(printer, doc, kind, title, original=False):
    if printer.kind == 'receipt':
        # 2 copies of a completed receipt on the receipt printer; Letter paper (below) and Create PDF print 1.
        return send(printer, receipt_job(doc, pages(doc, kind, original)[0][0]))
    from .letter_printing import send_letter
    return send_letter(printer, pages(doc, kind, original, letter=True)[0], title)


def pdf(doc, kind, title, original=False):
    images, dpi = pages(doc, kind, original)
    stream = io.BytesIO()
    first, rest = images[0], images[1:]
    first.save(stream, format='PDF', resolution=dpi, save_all=True, append_images=rest, title=title, author=business().name)
    return stream.getvalue()
