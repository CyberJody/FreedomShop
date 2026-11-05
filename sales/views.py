# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import secrets
import uuid
from datetime import datetime, time as dtime, timedelta

from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from customers.models import Customer, tax_location_for
from .models import DiscountReason, Document, Item, Line, PAYMENT_METHODS, CARD_TYPES, RESTOCKING_FEE_PERCENT, RETURN_REASONS, plain, exact_text, quantity_text, rate_text, money as round_money, store_today
from config.lists import sorted_list
from config.list_scroll import wants_rows, scroll_rows, scroll_config, link, fmt_date, qty, money
from . import accounts
from accounts import admin_mode
from .services import save_document, WorkflowError, save_order_payment, fulfill_order, returnable, return_status, default_line_rate
from . import holds
from .printing import PrintError, receipt_printers, print_receipt as send_receipt


def account_info(customer):
    """Charge-account details the register needs for the Account dialog."""
    if not customer:
        return dict(charge_account=False, account_balance='0.00', account_limit='0.00')
    return dict(charge_account=customer.charge_account, account_balance=str(customer.account_balance), account_limit=str(customer.account_limit))


def return_info(line):
    """Accept Return (B1): what the register shows on a return line (the original receipt and the quantity sold there)."""
    original = line.returned_from_line
    if original is None:
        return {}
    available, _ = return_status(original, exclude_document=line.document)
    return dict(returned_from_line=original.pk, return_receipt=original.document.number,
                return_limit=quantity_text(available), damaged=line.damaged, return_reason=line.return_reason,
                return_note=line.return_note, return_discount_percent=rate_text(original.document.discount_percent or 0),
                return_discount_share=plain(original.receipt_discount or 0), return_sold_amount=plain(original.amount))


def customer_result(c):
    """A customer as the register's customer search returns it."""
    return dict(id=c.pk, label=c.display_name, detail=' · '.join(filter(None, [c.phone, c.city, c.company])), notes=c.notes,
                accept_checks=c.accept_checks, tax_exempt=c.tax_exempt, tax_rate=rate_text(c.tax_rate) if c.tax_rate is not None else None,
                shipping_address=c.default_shipping_address, addresses=list(c.addresses.values('label', 'address')),
                # the register asks for a missing phone/address (Save or Skip) when the customer is put on a sale.
                contact=dict(version=c.version, phones=c.phone_numbers(), missing_phone=not c.phone_numbers(),
                             missing_address=not c.has_address, street=c.street, city=c.city, state=c.state,
                             postal_code=c.postal_code), **account_info(c))


def override_by(request):
    """Who made an Admin Override: the signed-in admin (only admins can override)."""
    user = getattr(request, 'user', None)
    return user.get_username() if user is not None and user.is_authenticated else ''


def qb_recorded(doc):
    """The register's copy of what services.save_document compares on an open QuickBooks order (review, finding 6):
    the order's recorded subtotal/discount/tax/total, its discount percent and tax-exempt flag, each saved line's
    amount / discount share / tax rate, and whether QB's discount is a typed dollar amount (discounts.qb_typed_discount).
    None for every other document, which the register totals the ordinary way."""
    if doc.kind != 'order' or not doc.from_quickbooks or doc.status != 'open':
        return None
    from .discounts import qb_typed_discount
    lines = [line for line in doc.lines.all() if not line.restocking_fee]
    return dict(subtotal=plain(doc.subtotal), discount=plain(doc.discount or 0), tax=plain(doc.tax) if doc.tax is not None else None,
                total=plain(doc.total) if doc.total is not None else None, discount_percent=rate_text(doc.discount_percent or 0),
                tax_exempt=doc.tax_exempt, typed_discount=qb_typed_discount(doc, lines),
                lines={str(line.pk): dict(amount=plain(line.amount), share=plain(line.receipt_discount or 0),
                                          tax_rate=rate_text(line.tax_rate) if line.tax_rate is not None else None) for line in lines})


def document_data(doc):
    lines = list(doc.lines.select_related('item', 'returned_from_line__document').all())
    # Available beside On Hand on each line = on hand - open orders - held, the same figure as the Item List
    # (this receipt's own hold counts too, so the register never shows a different Available than inventory).
    free = holds.available_by_item([l.item for l in lines if l.item])
    return {
        'token': str(doc.token), 'version': doc.version, 'kind': doc.kind,
        'payments':doc.payment_breakdown, 'customer_po':doc.customer_po, 'payment_method': doc.payment_method, 'tendered': str(doc.tendered or ''), 'tax_exempt': doc.tax_exempt, 'status': doc.status, 'number': doc.number, 'customer_id': doc.customer_id,
        'imported': doc.imported, 'subtotal': plain(doc.subtotal), 'discount': plain(doc.discount), 'tax': plain(doc.tax), 'total': plain(doc.total),
        'customer_name': doc.customer_name, 'notes': doc.notes,
        # hold state for the register's Put on Hold dialog and the held banner.
        'holds_stock': doc.holds_stock, 'hold_until': str(doc.hold_date or ''), 'hold_expired': doc.hold_expired if doc.status == 'held' else False,
        'hold_label': doc.hold_label if doc.status == 'held' else '',
        # Hold for Pickup on the checkout screen (the box and its optional est. pickup date).
        'pickup': dict(held=doc.hold_for_pickup, est_pickup_date=str(doc.est_pickup_date or ''),
                       picked_up=doc.picked_up_at is not None) if doc.kind == 'sale' else None,
        'unused_deposit': plain(doc.unused_payments) if doc.kind == 'order' else '0.00',
        'order_pickup': doc.order_pickup if doc.kind == 'order' else None,  # the order's Hold for Pickup box
        'pickup_receipt': bool(doc.source_order_id),
        # review, finding 6: an open order from QuickBooks shows QB's recorded figures until its first money edit,
        # exactly as services.save_document keeps them; a typed-dollar QB discount keeps its amount across edits.
        'qb_recorded': qb_recorded(doc),
        'discount_percent': str(doc.discount_percent), 'discount_reason': doc.discount_reason, 'discount_note': doc.discount_note,
        'customer_notes':doc.customer.notes if doc.customer else '',
        **account_info(doc.customer),
        'account_effect': str(accounts.effect(doc)),
        'customer_tax_rate':rate_text(doc.customer.tax_rate) if doc.customer and doc.customer.tax_rate is not None else None,
        'customer_addresses':list(doc.customer.addresses.values('label','address')) if doc.customer else [],
        'customer_default_shipping':doc.customer.default_shipping_address if doc.customer else '',
        'shipping_address': doc.shipping_address, 'due_date': str(doc.due_date or ''),
        # The restocking fee is not an editable line: the register shows it from the flag and the server recomputes it.
        'restocking_fee': any(line.restocking_fee for line in doc.lines.all()),
        'restocking_fee_lines': [dict(item_name=line.item_name, description=line.description, quantity=quantity_text(line.quantity),
                                      price=plain(line.price), amount=plain(line.amount),
                                      tax_rate=rate_text(line.tax_rate) if line.tax_rate is not None else None)
                                 for line in doc.lines.all() if line.restocking_fee],
        # `price` is the exact unit price (up to 4 decimals after a % discount) for the register's arithmetic;
        # `display_price` is what the screen shows (whole cents). `amount` is the gross extension, QB's Ext. column.
        'lines': [dict(id=line.pk, item_id=line.item_id, sku=line.sku, item_name=line.item_name,
                       description=line.description, vendor=line.vendor, certificate_number=line.certificate_number,
                       quantity=quantity_text(line.quantity), price=exact_text(line.price), display_price=plain(line.price), amount=plain(line.amount),
                       original_price=plain(line.original_price), discount_percent=rate_text(line.discount_percent or 0), discount_amount=plain(line.dollars_off) if line.dollars_off is not None else '0', receipt_discount=plain(line.receipt_discount or 0),
                       order_line=line.source_order_line_id,  # a pickup line keeps the share its order gave it
                       discount_reason=line.discount_reason.replace('Price Descrepancy', 'Price Discrepancy'), discount_note=line.discount_note,
                       tax_rate=rate_text(line.tax_rate) if line.tax_rate is not None else None,
                       account_payment=line.item_id is None and line.item_name == accounts.POA_NAME,
                       on_hand=quantity_text(line.item.on_hand) if line.item and line.item.on_hand is not None else None,
                       available=quantity_text(free[line.item_id]) if line.item and free.get(line.item_id) is not None else None,
                       **return_info(line))
                  for line in lines if not line.restocking_fee],
    }


# short "saved" notes shown on the order after its payment forms post back (?saved=<key>).
SAVED_NOTES = {'payment-recorded': 'Payment recorded.', 'refund-recorded': 'Refund recorded.',
               'payment-saved': 'Payment saved.', 'payment-deleted': 'Payment deleted.',
               'order-cancelled': 'Order cancelled.',
               'pickup-held': 'Held for pickup.', 'pickup-date': 'Est. pickup date changed.',
               'pickup-done': 'Marked picked up.'}


def screen_tenders(doc):
    """the finished receipt's payment line on screen. An Account tender is money put on the customer's account,
    not money received, so it reads 'Charged to account: $X' (a refund to the account: 'Credited to account: $X'),
    followed once by 'Account balance: $Y', the balance after this receipt (the printed receipt's 'Account Balance').
    A payment on account keeps '<tender> received' and also shows the balance after. The printout is unchanged."""
    if not doc or doc.imported or not doc.payment_method:
        return {'screen_tenders': [], 'account_balance_after': None}
    rows = []
    for tender in doc.tender_lines:
        if tender.get('change'):
            text = 'Change'
        elif tender.get('method') == 'account':
            text = 'Charged to account' if tender['amount'] >= 0 else 'Credited to account'
        else:
            text = f"{tender['label']} received"
        rows.append(dict(text=text, amount=abs(tender['amount']) if tender.get('method') == 'account' else tender['amount']))
    after = accounts.balances(doc)[1] if accounts.shows_account(doc) else None
    return {'screen_tenders': rows, 'account_balance_after': after}


@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def editor(request, kind, token=None):
    if request.method == 'POST':
        try:
            if len(request.body) > 200000:
                raise WorkflowError('This document is too large.')
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise WorkflowError('Invalid document.')
            if token is not None and str(token) != str(data.get('token')):
                raise WorkflowError('Document does not match this page.')
            override = None
            if data.get('override') is True:
                # Admin Override of a locked (past-day) receipt: signed-in admins only, always with a typed reason;
                # logged as a ReceiptOverride.
                if not admin_mode.is_on(request):
                    raise WorkflowError('Sign in as an admin to override a past receipt.')
                override = dict(reason=data.get('override_reason'), by=override_by(request),
                                address=request.META.get('REMOTE_ADDR', ''))
            from accounts.cashiers import active
            cashier = active(request)
            doc = save_document(data, kind, token or data.get('token'), override=override,
                                cashier=cashier.name if cashier else None)
            if kind == 'order' and data.get('action') == 'cancel' and doc.status == 'cancelled':
                return JsonResponse({'url': f'/pos/order/{doc.token}/?saved=order-cancelled', 'number': doc.number})
            return JsonResponse({'url': f'/pos/{kind}/{doc.token}/', 'number': doc.number,
                                 'message': ' '.join(getattr(doc, 'notes_for_screen', None) or [])})  # notes
        except (WorkflowError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return JsonResponse({'error': str(exc)}, status=400)
        except IntegrityError:
            return JsonResponse({'error': 'This document was saved by another request. Reload before continuing.'}, status=409)
    doc = get_object_or_404(Document, token=token, kind=kind) if token else None
    locked = bool(doc and doc.locked)
    # a cancelled receipt is finished: no Cancel, Put on Hold or Save on it (not even an Admin Override, which
    # would bring it back as a sale); it opens read-only like a locked one. Print stays.
    # a cancelled sales order is finished the same way (no Save, Cancel, payments or pickups; Print stays).
    finished = bool(doc and doc.kind in ('sale', 'order') and doc.status == 'cancelled')
    overriding = locked and not finished and admin_mode.is_on(request) and request.GET.get('override') == '1'
    if doc:
        initial = document_data(doc)
        initial['readonly'] = bool(doc.imported or finished or (locked and not overriding))
        initial['override'] = overriding
    else:
        customer_id = request.GET.get('customer', '')
        customer = Customer.objects.filter(pk=customer_id, active=True).first() if customer_id.isdigit() and len(customer_id) <= 18 else None
        initial = dict(token=str(uuid.uuid4()), version=0, kind=kind, lines=[],
                       customer_id=customer.pk if customer else None,
                       customer_name=customer.display_name if customer else '',
                       notes='', order_pickup=True if kind == 'order' else None, customer_notes=customer.notes if customer else '', shipping_address=customer.default_shipping_address if customer else '', due_date='', tax_exempt=customer.tax_exempt if customer else False, customer_tax_rate=rate_text(customer.tax_rate) if customer and customer.tax_rate is not None else None, **account_info(customer))
    initial['restocking_fee_percent'] = rate_text(RESTOCKING_FEE_PERCENT)
    if not doc and kind == 'sale' and request.GET.get('account_payment') == '1' and customer:
        # "Take Payment for Charge Account" from a sales order: open the Charge Account dialog on a new receipt.
        initial['open_account'] = 'pay'
        order_token = request.GET.get('order', '')
        order = Document.objects.filter(token=order_token, kind='order', customer=customer).first() if len(order_token) == 36 else None
        if order:
            initial['notes'] = f'Payment on account taken with sales order {order.number}'
    if not doc and kind == 'sale':
        # Accept Return (B1): from a receipt (Sales History / receipt screen) or with a receipt search.
        source = request.GET.get('return_from', '')
        if len(source) == 36 and Document.objects.filter(token=source, kind='sale', status='completed').exists():
            initial['return_from'] = source
        elif request.GET.get('accept_return') == '1':
            initial['open_return'] = True
    if not doc:
        initial['customer_addresses']=list(customer.addresses.values('label','address')) if customer else []
        initial['customer_default_shipping']=customer.default_shipping_address if customer else ''
        ids=request.GET.getlist('item')
        if len(ids)>100 or any(not x.isdigit() or len(x)>18 for x in ids): raise Http404
        picked=list(Item.objects.filter(pk__in=ids,active=True));free=holds.available_by_item(picked)
        initial['lines']=[dict(item_id=i.pk,sku=i.sku,item_name=i.display_name,description=i.description,vendor=i.vendor,quantity='1',price=plain(i.price),original_price=plain(i.price),tax_rate=rate_text(customer.tax_rate) if customer and customer.tax_rate is not None else (rate_text(i.tax_rate) if i.tax_rate is not None else rate_text(default_line_rate())),on_hand=str(i.on_hand) if i.on_hand is not None else None,available=quantity_text(free[i.pk]) if free[i.pk] is not None else None) for i in picked]
    return render(request, 'sales/editor.html', {
        'title': 'Sales Order' if kind == 'order' else 'Sales Receipt',
        'kind': kind, 'doc': doc, 'initial': initial,
        'readonly': bool(doc and (doc.imported or finished or (locked and not overriding))),
        'locked': locked and not finished, 'overriding': overriding, 'admin_mode_on': admin_mode.is_on(request),
        'overrides': doc.overrides.all() if doc else [],
        'can_return': bool(doc and doc.kind == 'sale' and doc.status == 'completed' and doc.receipt_type in (None, 0)
                           and doc.lines.filter(quantity__gt=0).exists()), 'payment_methods': PAYMENT_METHODS, 'card_types': CARD_TYPES, 'payment_token': uuid.uuid4(), 'pickup_token': uuid.uuid4(),
        'suggested_deposit': round_money((doc.total or 0) / 4) if doc else 0,  # 25 %, half-up to the cent as QB rounds
        'order_payments': doc.payments.all() if doc and kind=='order' else [],
        'payment_changes': doc.payment_changes.all() if doc and kind=='order' else [],
        'today': store_today(),
        'tenders': doc.tenders.all() if doc and doc.imported else [],
        **screen_tenders(doc),
        'workflow_error': request.GET.get('error',''), 'return_reasons': RETURN_REASONS,
        'saved_note': SAVED_NOTES.get(request.GET.get('saved', '')) if doc else None,
        'discount_reasons': list(DiscountReason.objects.filter(active=True).values_list('name', flat=True)),
        'receipt_printers': receipt_printers(),
        'inventory_count': Item.objects.filter(active=True).count(),
        'hold_choices': holds.HOLD_CHOICES, 'hold_default': holds.DEFAULT_PRESET,  # Put on Hold dialog
        **pickup_context(doc),
        'quick_groups':Item.objects.filter(active=True).exclude(quick_pick_group='').values_list('quick_pick_group',flat=True).distinct(),
        **special_item_choices(kind, doc),
    })


def pickup_context(doc):
    """what the receipt screen's pickup strip shows: `pickup_strip` on completed sales receipts that are held for
    pickup or could be, with the Hold for Pickup button when `pickup_can_hold`."""
    if not doc or doc.kind != 'sale':
        return {'pickup_strip': False, 'pickup_can_hold': False}
    from .pickup import ineligible_reason
    can_hold = doc.status == 'completed' and not doc.hold_for_pickup and not ineligible_reason(doc)
    return {'pickup_strip': doc.status == 'completed' and (doc.hold_for_pickup or can_hold), 'pickup_can_hold': can_hold}


@never_cache
@require_http_methods(['POST'])
def pickup_action(request, token):
    """Hold for Pickup / change the est. pickup date / Mark Picked Up on a finished receipt. No money, tax or stock
    changes, so any day and any login (no receipt lock, no Admin Override). Mark Picked Up stamps the active cashier."""
    from urllib.parse import urlencode
    from accounts.cashiers import active
    from . import pickup
    action = request.POST.get('action', '')
    # Mark Picked Up from the Awaiting Pickup screen goes back there (only that fixed page, never a posted URL).
    back = request.POST.get('next') == 'awaiting' and action == 'picked_up'
    try:
        cashier = active(request)
        doc = pickup.act(token, action, request.POST.get('est_pickup_date', ''), cashier=cashier.name if cashier else None,
                         version=request.POST.get('version'))
    except pickup.PickupError as exc:
        if back:
            return redirect('/pos/awaiting-pickup/?' + urlencode({'error': str(exc)}))
        return redirect(f'/pos/sale/{token}/?' + urlencode({'error': str(exc)}))
    if back:
        return redirect('/pos/awaiting-pickup/?' + urlencode({'done': doc.numbered_label}))
    saved = {'hold': 'pickup-held', 'date': 'pickup-date', 'picked_up': 'pickup-done'}[action]
    return redirect(f'/pos/sale/{token}/?saved={saved}')


@require_safe
@never_cache
def awaiting_pickup(request):
    """Awaiting Pickup. Paid sales held for pickup and open sales orders with goods in the
    store (sales.awaiting). orders count only goods received on a receiving voucher made in this app,
    which keeps the QuickBooks backlog off the page, so there is no date filter."""
    from . import awaiting
    today = timezone.localdate()
    receipts = awaiting.awaiting_receipts(today)
    orders = awaiting.ready_orders(today)
    return render(request, 'sales/awaiting-pickup.html', dict(
        title='Awaiting Pickup', today=today, receipts=receipts, orders=orders,
        done=request.GET.get('done', '')[:60], error=request.GET.get('error', '')[:300]))


def special_item_choices(kind, doc):
    """the Add Special Order Item pop-up's Department and Vendor lists (active ones), defaulting to SPE."""
    if kind != 'order' or (doc and doc.imported):
        return {}
    from purchasing.models import Department, Vendor
    from purchasing.item_views import special_order_department
    spe = special_order_department()
    return {'special_departments': Department.objects.filter(active=True).order_by('name'),
            'special_department_default': spe.pk if spe else None,
            'special_vendors': Vendor.objects.filter(active=True).order_by('company', 'code')}


@require_safe
@never_cache
def lookup(request, kind):
    query = request.GET.get('q', '').strip()[:100]
    group=request.GET.get('group','')[:100]
    if len(query) < (1 if kind == 'items' else 2) and not (kind=='items' and group):
        return JsonResponse({'results': []})
    if kind == 'customers':
        records = Customer.objects.filter(active=True)
        for term in query.split():
            records = records.filter(Q(full_name__icontains=term) | Q(company__icontains=term) | Q(phone__icontains=term) | Q(customer_number__icontains=term) | Q(street__icontains=term) | Q(postal_code__icontains=term) | Q(email__icontains=term) | Q(mobile__icontains=term) | Q(alternate_phone__icontains=term))
        records = records[:20]
        data = [customer_result(c) for c in records]
    else:
        from purchasing.item_views import search_items
        records = search_items(Item.objects.filter(active=True), query)
        if group: records=records.filter(quick_pick_group=group)
        records=list(records[:100 if group else 20])
        free = holds.available_by_item(records)  # what staff can actually sell
        data = [dict(id=i.pk, label=i.display_name, sku=i.sku, description=i.description, price=plain(i.price), vendor=i.vendor,
                     on_hand=quantity_text(i.on_hand) if i.on_hand is not None else None,
                     available=quantity_text(free[i.pk]) if free[i.pk] is not None else None,
                     tax_rate=rate_text(i.tax_rate) if i.tax_rate is not None else None) for i in records]
    return JsonResponse({'results': data})


@require_safe
@never_cache
def document_list(request, section):
    title, kind, state = {
        'history': ('Sales History', 'sale', 'completed'),
        'held': ('Held Receipts', 'sale', 'held'),
        'orders': ('Sales Order List', 'order', 'open'),
    }[section]
    query = request.GET.get('q', '').strip()[:100]
    period = request.GET.get('period', 'ytd' if section == 'held' else 'all')  # QB Held Receipts opens on Year to Date
    status = request.GET.get('status', 'all' if section == 'orders' else state)
    documents = Document.objects.filter(kind=kind)
    if section == 'held':
        holds.expire_overdue_holds()  # mark holds whose time ran out before listing them
    customer = None
    if request.GET.get('customer'):
        key = request.GET['customer']
        if not key.isdigit() or len(key) > 18:
            raise Http404
        customer = get_object_or_404(Customer, pk=key)
        documents = documents.filter(customer=customer)
    if section == 'history' and status == 'all':
        documents=documents.exclude(status='held')
    elif section == 'history' and status == 'cancelled':
        documents=documents.filter(status='cancelled')
    elif section == 'history' and status == 'pickup':
        # only receipts whose goods are still waiting in the store (sorted by est. pickup date below).
        documents=documents.filter(status='completed', hold_for_pickup=True, picked_up_at__isnull=True)
    elif section == 'orders' and status == 'all':
        pass
    elif section == 'orders' and status in {'cancelled','completed'}:
        documents = documents.filter(status=status)
    else:
        status = state
        documents = documents.filter(status=state)
    if query:
        match = Q(customer_po__icontains=query) | Q(customer_name__icontains=query) | Q(lines__description__icontains=query) | Q(lines__sku__icontains=query) | Q(lines__item_name__icontains=query)
        number = query.upper().removeprefix('SO-').removeprefix('R-').removeprefix('H-')
        if number.isdigit() and len(number) <= 18:
            numbered = Q(number_value=int(number)) | Q(source_number=int(number))
            match |= numbered
            # Legacy prefixed searches only; bare row IDs are not document numbers.
            if query.upper().startswith(('SO-', 'R-', 'H-')) and not documents.filter(numbered).exists():
                match |= Q(pk=int(number))
        documents = Document.objects.filter(pk__in=documents.filter(match).values('pk'))
    date_field = 'completed_at' if section == 'history' else 'created_at'
    if period in {'7', '30', '90'}:
        documents = documents.filter(**{date_field + '__gte': timezone.now() - timedelta(days=int(period))})
    elif period in {'today', 'ytd'}:
        today = timezone.localdate()
        first = today if period == 'today' else today.replace(month=1, day=1)
        documents = documents.filter(**{date_field + '__gte': timezone.make_aware(datetime.combine(first, dtime.min))})
    else:
        period = 'all'
    # Choose from the entire filtered receipt history, never orders or held receipts.
    # Opening a saved receipt uses its line snapshots; it does not create a sale.
    printable = documents.filter(status='completed', lines__isnull=False).distinct() if section == 'history' else Document.objects.none()
    printable_count = printable.count()
    if request.GET.get('random') == '1' and printable_count:
        receipt = printable.order_by('pk')[secrets.randbelow(printable_count)]
        return redirect('sale-detail', token=receipt.token)
    filtered = documents
    documents = documents.annotate(display_number=Coalesce('source_number', 'number_value'), item_count=Count('lines'), item_quantity=Sum('lines__quantity')).order_by('-completed_at' if section == 'history' else '-created_at', '-pk')
    date_field='completed_at' if section=='history' else 'created_at'
    fields=([''] if section=='orders' else [])+[date_field,'display_number','status','customer_name','item_count','item_quantity','total','due_date' if section=='orders' else 'payment_method']
    if section == 'held':
        # Held receipts have no issued number; keep their label column unsortable.
        fields=[date_field,'','status','receipt_type','workstation','cashier','customer_name','item_count','total','item_quantity','payment_method','hold_until','']
    if section == 'history':
        # QB POS Sales History sorts by Tax Location. Blank on older app receipts = from the exempt flag.
        from .history import tax_area
        documents = documents.annotate(tax_area=tax_area())
        # (matching QB): a Cashier column before Full Name, as QB's Sales History and the held list have it.
        fields.insert(fields.index('customer_name'), 'cashier')
        fields.append('tax_area')
    documents,sorting=sorted_list(request,documents,fields,'-'+date_field)
    if section == 'history' and status == 'pickup' and not request.GET.get('sort'):
        sorting['sort'] = 'pickup_due'  # soonest est. pickup date first, receipts with no date last
    if section == 'history':
        # (matching QB): the app's order deposits and refunds are listed beside receipts, as QB lists its deposit
        # receipts. Same filters (customer, date range, status, search); sorted together in one SQL UNION.
        from .history import MergedHistory, order_payments
        since = None
        if period in {'7', '30', '90'}:
            since = timezone.now() - timedelta(days=int(period))
        elif period in {'today', 'ytd'}:
            today = timezone.localdate()
            since = timezone.make_aware(datetime.combine(today if period == 'today' else today.replace(month=1, day=1), dtime.min))
        rows = wants_rows(request)

        def load(page_docs):
            page_docs = page_docs.annotate(display_number=Coalesce('source_number', 'number_value'), item_count=Count('lines'),
                                           item_quantity=Sum('lines__quantity'),
                                           tax_area=tax_area())
            page_docs = page_docs.select_related('customer').prefetch_related('tenders', *(('lines', 'payments', 'source_order__payments') if rows else ()))
            return mark_account_owed(list(page_docs))

        documents = MergedHistory(filtered, order_payments(customer, status, query, since), sorting['sort'], load)
        if rows:
            return scroll_rows(request, documents, lambda doc: sales_row(doc, section))
        page = Paginator(documents, 50).get_page(request.GET.get('page'))
    if section != 'history' and wants_rows(request):
        return scroll_rows(request, documents.prefetch_related('lines','payments','tenders','source_order__payments'),
                           lambda doc: sales_row(doc, section))
    if section != 'history':
        page = Paginator(documents, 50).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    params.pop('random', None)
    columns = len(fields) + (0 if section == 'held' else 1 + (3 if section == 'orders' else 0))
    return render(request, 'sales/held-list.html' if section == 'held' else 'sales/list.html', dict(title=title, section=section, page=page,
                  query=query, period=period, status=status, customer=customer,
                  printable_count=printable_count, **sorting, params=params.urlencode(),
                  list_scroll_config=scroll_config(request, page, columns, checkbox='order' if section == 'orders' else '')))


def held_type(doc):
    return doc.get_receipt_type_display() if doc.receipt_type is not None else 'Sales'


def mark_account_owed(docs):
    """set `account_owed` on receipts whose Account part is still unpaid (from the shared tax ledger's FIFO
    state, nothing stored) so Sales History can show 'On account · $X owed' instead of Completed."""
    from reports.tax_ledger import owed_by_receipt
    owed = owed_by_receipt(docs)
    for doc in docs:
        doc.account_owed = owed.get(doc.pk)
        doc.status_label = status_text(doc)
    return docs


def status_text(doc):
    from .pickup import history_text
    pickup = history_text(doc) if getattr(doc, 'kind', None) == 'sale' else ''
    owed = getattr(doc, 'account_owed', None)
    if owed:
        return ' · '.join(filter(None, [pickup, f'On account · ${owed:,.2f} owed']))
    return pickup or doc.get_status_display()


def sales_row(doc, section):
    href = f'/pos/{doc.kind}/{doc.token}/'
    if getattr(doc, 'is_order_payment', False):
        # an order deposit or refund taken in the app; the row opens its sales order.
        cells = [fmt_date(doc.completed_at), [link(doc.number_or_label, href)], doc.status_label, doc.cashier, doc.customer_name, '', '',
                 money(doc.total), doc.payment_label, doc.tax_area, [link('Show Details', href)]]
        return dict(id=f'payment-{doc.pk}', token=str(doc.token), name=f'{doc.number_or_label} · {doc.customer_name}', href=href, cells=cells)
    if section == 'held':
        cells = [fmt_date(doc.created_at), [link(doc.number_or_label, href)], 'Regular', held_type(doc), '' if doc.workstation is None else str(doc.workstation),
                 doc.cashier, doc.customer_name, str(doc.item_count), money(doc.total), qty(doc.item_quantity), doc.payment_label, doc.hold_label, [link('Unhold', href)]]
        return dict(id=doc.pk, token=str(doc.token), name=f'{doc.number_or_label} · {doc.customer_name}', href=href, cells=cells)
    status = status_text(doc) + (f' · {doc.type_label}' if doc.type_label else '')
    if section == 'history':
        returns = [l for l in doc.lines.all() if l.returned_from_line_id]
        if returns:
            reasons = list(dict.fromkeys(l.return_reason_text for l in returns if l.return_reason_text))
            status += ' · Return' + (f" ({'; '.join(reasons)})" if reasons else '')
    cells = [fmt_date(doc.completed_at if section == 'history' else doc.created_at), [link(doc.number_or_label, href)],
             status, *([doc.cashier] if section == 'history' else []), doc.customer_name,
             str(doc.item_count), qty(doc.item_quantity), money(doc.total) + (' + tax' if doc.tax is None else ''),
             fmt_date(doc.due_date) if section == 'orders' else doc.payment_label]
    if section == 'history':
        cells.append(getattr(doc, 'tax_area', '') or doc.tax_location or tax_location_for(doc.tax_exempt))
    if section == 'orders':
        cells += [money(doc.paid), money(doc.balance_due), qty(doc.unfilled)]
    cells.append([link('Unhold' if section == 'held' else 'Show Details', href)])
    return dict(id=doc.pk, token=str(doc.token), name=f'{doc.number_or_label} · {doc.customer_name}', href=href, cells=cells)


@never_cache
@require_http_methods(['POST'])
def order_action(request, token, action):
    from urllib.parse import urlencode
    order=get_object_or_404(Document,token=token,kind='order')
    try:
        if action=='payment':
            from accounts.cashiers import active
            cashier=active(request)
            save_order_payment(order.pk,request.POST,actor=dict(by=override_by(request),cashier=cashier.name if cashier else '',
                                                                 address=request.META.get('REMOTE_ADDR','')))
            # say so on the order ("Payment recorded." / "Payment saved." / "Payment deleted.").
            if request.POST.get('action')=='delete': saved='payment-deleted'
            elif request.POST.get('payment_id'): saved='payment-saved'
            else: saved='refund-recorded' if str(request.POST.get('amount','')).strip().startswith('-') else 'payment-recorded'
            return redirect(f'/pos/order/{token}/?saved={saved}')
        else:
            # the pickup receipt records who rang it, like any receipt (PIN cashier or signed-in admin).
            from accounts.cashiers import active
            cashier=active(request)
            receipt=fulfill_order(order.pk,request.POST,cashier=cashier.name if cashier else None)
            return redirect(f'/pos/sale/{receipt.token}/')
    except (WorkflowError, ValueError, IntegrityError) as exc:
        return redirect(f'/pos/order/{token}/?'+urlencode({'error':str(exc)}))
    return redirect(f'/pos/order/{token}/')


@never_cache
@require_http_methods(['POST'])
def print_receipt(request, token):
    doc = get_object_or_404(Document.objects.select_related('customer', 'source_order'), token=token, kind='sale')
    if doc.status not in {'completed', 'cancelled'}:
        return JsonResponse({'error': 'Complete the receipt before printing it to the receipt printer.'}, status=400)
    try:
        name = send_receipt(doc, request.POST.get('printer', ''), reprint=True)
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    return JsonResponse({'message': f'Sent {doc.numbered_label} to {name}.'})


def held_document(token):
    return get_object_or_404(Document.objects.select_related('customer'), token=token, kind='sale', status='held')


@require_safe
@never_cache
def held_details(request, token):
    """Show Details panel for one held receipt (QB POS Held Receipts > Show Details / F8)."""
    doc = held_document(token)
    lines = list(doc.lines.all())
    return render(request, 'sales/held-details.html', dict(doc=doc, lines=lines, receipt_type=held_type(doc),
                  quantity=sum((l.quantity for l in lines), 0), phone=(doc.customer.phone if doc.customer else ''),
                  hold_choices=holds.HOLD_CHOICES, hold_default=holds.DEFAULT_PRESET))


@require_http_methods(['POST'])
@never_cache
def held_extend(request, token):
    """extend (or shorten) a held receipt's hold from the Held Receipts list. A new hold time clears an Expired mark
    and reserves the items again; the lines are untouched."""
    from django.db import transaction
    from urllib.parse import urlencode
    with transaction.atomic():
        # Lock the document only: FOR UPDATE can't span the nullable customer join on Postgres.
        doc = get_object_or_404(Document.objects.select_for_update(), token=token, kind='sale')
        if doc.status != 'held':
            return redirect('/pos/held/?' + urlencode({'error': f'{doc.numbered_label} is no longer held.'}))
        choice = request.POST.get('hold_until', '')
        if choice == 'custom':
            choice = request.POST.get('hold_date', '')
        try:
            holds.apply_hold(doc, doc.customer, choice, was_held=False)  # a fresh choice is required; 'keep' is not an extension
        except holds.HoldError as exc:
            return redirect('/pos/held/?' + urlencode({'error': str(exc), 'held': str(doc.token)}))
        doc.version += 1
        doc.save(update_fields=['hold_until', 'holds_stock', 'hold_expired_at', 'version'])
    from django.http import QueryDict
    params = QueryDict(request.POST.get('next', '')[:1000], mutable=True)
    for key in ('deleted', 'error', 'held', 'extended'):
        params.pop(key, None)
    params['extended'] = f'{doc.customer_name}: {doc.hold_label.lower() if doc.holds_stock else "this receipt holds no stock"}'
    params['held'] = str(doc.token)
    return redirect('/pos/held/?' + params.urlencode())


@require_http_methods(['POST'])
@never_cache
def held_delete(request, token):
    """QB POS Held Receipts > Delete: a held receipt never moved stock or money, so it is removed outright. Deleting
    it also ends any hold it had on its items (the reservation lives on the held row, not in stock movements)."""
    from django.db import transaction
    from urllib.parse import urlencode
    with transaction.atomic():
        doc = get_object_or_404(Document.objects.select_for_update(), token=token, kind='sale')
        if doc.status != 'held':
            return redirect('/pos/held/?' + urlencode({'error': f'{doc.numbered_label} is no longer held.'}))  # finished elsewhere
        name = doc.customer_name
        doc.lines.all().delete()
        doc.delete()
    from django.http import QueryDict
    params = QueryDict(request.POST.get('next', '')[:1000], mutable=True)
    for key in ('deleted', 'error', 'held'):
        params.pop(key, None)
    params['deleted'] = name
    return redirect('/pos/held/?' + params.urlencode())


RETURN_EXCLUDED_TYPES = (1, 2, 3, 4)


@require_safe
@never_cache
def return_lookup(request):
    """Accept Return receipt search (QB: Accept Return with no receipt selected): number, customer name or phone."""
    query = request.GET.get('q', '').strip()[:100]
    if len(query) < 2 and not query.isdigit():
        return JsonResponse({'results': []})
    receipts = Document.objects.filter(kind='sale', status='completed').exclude(receipt_type__in=RETURN_EXCLUDED_TYPES)
    number = query.upper().removeprefix('R-')
    match = Q()
    if number.isdigit() and len(number) <= 18:
        match |= Q(number_value=int(number)) | Q(source_number=int(number))
    if len(query) >= 2:
        names = Q()
        for term in query.split():
            names &= Q(customer_name__icontains=term) | Q(customer__phone__icontains=term) | Q(customer__mobile__icontains=term)
        match |= names
    receipts = receipts.filter(match).filter(lines__quantity__gt=0).distinct().order_by('-completed_at', '-pk')[:20]
    results = [dict(token=str(d.token), label=d.numbered_label,
                    detail=' · '.join(filter(None, [timezone.localtime(d.completed_at).strftime('%-m/%-d/%Y') if d.completed_at else '',
                                                    d.customer_name, f'${plain(d.total)}' if d.total is not None else ''])))
               for d in receipts]
    return JsonResponse({'results': results})


@require_safe
@never_cache
def return_lines(request, token):
    """The original receipt's lines for the Take Return/Exchange pick list (sold quantity = the most that can be returned)."""
    doc = get_object_or_404(Document.objects.select_related('customer'), token=token, kind='sale')
    # The return receipt being edited (same day): its own saved return lines don't count against the cap.
    editing = request.GET.get('exclude', '')
    editing = Document.objects.filter(token=editing, kind='sale').first() if len(editing) == 36 else None
    if doc.status != 'completed' or doc.receipt_type in RETURN_EXCLUDED_TYPES:
        return JsonResponse({'error': f'{doc.numbered_label} is not a completed sales receipt, so nothing can be returned from it.'}, status=400)
    lines = []
    for line in doc.lines.select_related('item', 'document').all():
        if not returnable(line):
            continue
        available, returned = return_status(line, exclude_document=editing)
        lines.append(dict(id=line.pk, item_id=line.item_id, sku=line.sku, item_name=line.item_name, description=line.description,
                          vendor=line.vendor, sold=quantity_text(line.quantity), price=exact_text(line.price), display_price=plain(line.price),
                          discount_percent=rate_text(doc.discount_percent or 0),
                          # Sold on a pickup of a $-discounted order (percent 0): the line's own share, so a return
                          # refunds the discounted amount like a %-discounted receipt's does.
                          discount_share=plain(line.receipt_discount or 0), sold_amount=plain(line.amount),
                          available=quantity_text(available), returned=quantity_text(returned),
                          tax_rate=rate_text(line.tax_rate) if line.tax_rate is not None else None,
                          on_hand=quantity_text(line.item.on_hand) if line.item and line.item.on_hand is not None else None))
    customer = doc.customer if doc.customer and doc.customer.active else None
    when = timezone.localtime(doc.completed_at).strftime('%-m/%-d/%Y') if doc.completed_at else ''
    return JsonResponse({'token': str(doc.token), 'number': doc.number, 'label': doc.numbered_label, 'date': when,
                         'customer_name': doc.customer_name, 'tax_exempt': doc.tax_exempt,
                         'customer': customer_result(customer) if customer else None, 'lines': lines})
