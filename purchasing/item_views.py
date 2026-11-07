# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editable inventory screens."""
from shop_settings.permissions import register_can, register_required, restrict_item_form, refused
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from urllib.parse import urlencode
from .item_scrolling import item_return_url
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe, require_http_methods
from django.views.decorators.clickjacking import xframe_options_sameorigin

from sales.models import Item, Line, StockMovement, InventoryAdjustment, money, quantity_text, rate_text
from .forms import ItemForm, ItemVendorFormSet
from .models import PurchaseLine, ReceivingMovement


SEARCH_FIELDS = ('name', 'sku', 'description', 'upc', 'alu', 'wood', 'size', 'manufacturer', 'location', 'style')


def search_items(queryset, query):
    for term in query.split():
        match = Q()
        for field in SEARCH_FIELDS:
            match |= Q(**{field + '__icontains': term})
        match |= Q(alternate_vendors__upc__icontains=term) | Q(alternate_vendors__alu__icontains=term)
        queryset = queryset.filter(match)
    return queryset.defer('photo', 'comments').distinct()


GROUPS = {
    'basic': ['item_type', 'department', 'description', 'size', 'wood', 'style', 'price',
              'average_cost', 'on_hand', 'adjustment_note', 'tax_category', 'tax_rate', 'upc', 'quick_pick_group'],
    'more': ['primary_vendor', 'order_cost', 'reorder_point', 'sku', 'alu', 'unit_of_measure',
             'manufacturer', 'location', 'oil_su', 'reverse_notavail', 'pull', 'finish'],  # 'active' is the Items Trash, not a checkbox
    'shipping': ['print_tags', 'unorderable', 'use_serial_numbers', 'earn_commission', 'earn_rewards',
                 'shipping_weight', 'shipping_weight_unit', 'shipping_height', 'shipping_length',
                 'shipping_width', 'shipping_dimension_unit'],
    'pricing': ['price_2', 'price_3', 'price_4', 'price_5'],
}


def field_groups(form):
    return {group + '_fields': [dict(field=form[name], name=name, label=form[name].label,
        known=False, value=None, checkbox=form[name].field.widget.input_type == 'checkbox'
        if hasattr(form[name].field.widget, 'input_type') else False) for name in names if name in form.fields]
        for group, names in GROUPS.items()}


def activity(item):
    sales = list(Line.objects.filter(item=item, document__kind='sale', document__status='completed'))
    net_sales = sum((line.amount for line in sales), Decimal('0'))
    costs_known = all(line.unit_cost is not None for line in sales)
    cogs = sum((money(line.quantity * line.unit_cost) for line in sales), Decimal('0')) if costs_known else None
    profit = net_sales - cogs if cogs is not None else None
    received = ReceivingMovement.objects.filter(item=item).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
    sold = sum((line.quantity for line in sales), Decimal('0'))
    from .catalog_quantities import on_order_for
    on_order = on_order_for(item)  # open PO lines on POs dated 2026 or later (catalog_quantities' rule)
    customer_orders = sum((max(line.quantity-line.fulfilled, Decimal('0')) for line in Line.objects.filter(item=item, document__kind='order', document__status='open')), Decimal('0'))
    # only app-made holds for real customers reserve stock (sales.holds); QB held receipts, set signs and quotes
    # are listed for reference but never reduce Available.
    from sales.holds import held_by_item
    held = held_by_item([item.pk]).get(item.pk, Decimal('0'))
    held_all = Line.objects.filter(item=item, document__kind='sale', document__status='held').aggregate(total=Sum('quantity'))['total'] or Decimal('0')
    history = [dict(date=m.created_at, kind='Sale', quantity=m.quantity, number=m.document.number,
                    url=f'/pos/sale/{m.document.token}/')
               for m in StockMovement.objects.filter(item=item).select_related('document').order_by('-created_at')[:50]]
    history += [dict(date=m.created_at, kind='Receiving', quantity=m.quantity, number=m.document.number,
                     url=f'/purchasing/voucher/{m.document.token}/')
                for m in ReceivingMovement.objects.filter(item=item).select_related('document').order_by('-created_at')[:50]]
    history += [dict(date=a.created_at, kind='Adjustment', quantity=a.quantity_change, number=f'ADJ-{a.pk}', url=f'/purchasing/items/{item.pk}/#adjustments') for a in item.adjustments.all()[:50]]
    return dict(adjustments=item.adjustments.all()[:100], history=sorted(history, key=lambda row: row['date'], reverse=True)[:50],
                net_sales=net_sales, cogs=cogs, profit=profit,
                margin=(profit / net_sales * 100) if profit is not None and net_sales else None,
                received=received, sold=sold, on_order=on_order, customer_orders=customer_orders, held=held, held_all=held_all,
                # QB POS: Available Qty = On Hand - quantity on customer orders (verified against all 2,370 items in the
                # 9/27 backup), less what app-made held receipts reserve (Available = On Hand - open order qty - held).
                available=item.on_hand - customer_orders - held if item.on_hand is not None else None)


def same_name_items(name, exclude_pk=None):
    """other ACTIVE items whose name equals `name`, case-insensitive and trimmed (item numbers)."""
    from django.db.models.functions import Lower, Trim
    key = (name or '').strip().lower()
    if not key:
        return []
    found = Item.objects.annotate(name_key=Lower(Trim('name'))).filter(active=True, name_key=key)
    if exclude_pk:
        found = found.exclude(pk=exclude_pk)
    return list(found.order_by('sku').values_list('sku', flat=True)[:4])


def same_name_message(skus):
    """'Item #6045 already uses this name. Save anyway?' (several: 'Items #6045, #7001 already use...')."""
    if len(skus) == 1:
        return f'Item #{skus[0]} already uses this name. Save anyway?'
    shown = ', '.join(f'#{sku}' for sku in skus[:3]) + (' and others' if len(skus) > 3 else '')
    return f'Items {shown} already use this name. Save anyway?'


@never_cache
@require_safe
@register_required('edit_items')
def item_name_check(request):
    """the item form asks before saving a name another active item already uses (the server re-checks on save)."""
    from django.http import JsonResponse
    exclude = request.GET.get('item', '')
    exclude = int(exclude) if exclude.isdigit() and len(exclude) <= 18 else None
    skus = same_name_items(request.GET.get('name', '')[:200], exclude)
    return JsonResponse(dict(skus=skus, message=same_name_message(skus) if skus else ''))


def save_item_form(form, pk, version, original_stock, original_cost, vendors=None):
    """Save a valid ItemForm the Item List's way (item_edit and Add Special Order Item on a sales order): a new
    item gets the next item number and version 1, the vendor code follows the primary vendor, and a stock/cost change
    is recorded as an adjustment (a new item's starting 0 with a blank cost is not one)."""
    with transaction.atomic():
        item = form.save(commit=False)
        if not pk:
            from .item_numbers import next_item_number
            item.sku = next_item_number()
        item.version = version + 1 if pk else 1
        item.vendor = item.primary_vendor.code if item.primary_vendor else ''
        item.save()
        starting_zero = not pk and item.on_hand == 0 and item.average_cost is None
        if (original_stock, original_cost) != (item.on_hand, item.average_cost) and not starting_zero:
            InventoryAdjustment.objects.create(item=item,old_quantity=original_stock,new_quantity=item.on_hand,old_cost=original_cost,new_cost=item.average_cost,note=form.cleaned_data.get('adjustment_note',''))
        if vendors is not None:
            vendors.instance = item
            vendors.save()
    return item


def special_order_department():
    """a shop's special-order department (QB code SPE, \"SPECIAL ORDER\"), found by code, else by name."""
    from .models import Department
    active = Department.objects.filter(active=True)
    return active.filter(code__iexact='SPE').order_by('pk').first() \
        or active.filter(name__iexact='special order').order_by('pk').first()


def new_item_tax():
    """A new item's tax category and rate with no department: the shop's taxable category at the default rate."""
    from shop_settings.config import tax
    config = tax()
    from config.tax import coherent_tax
    category, rate = coherent_tax(config.taxable_category.name, None)
    return {'tax_category': category, 'tax_rate': rate}


def new_item_form_data():
    """The values a blank Add Item form posts (its initial values), so an item made outside that form gets the same
    defaults (tax, print tags,...)."""
    form = ItemForm(instance=Item(), initial={**new_item_tax(), 'on_hand': None})
    data = {}
    for name in ItemForm.Meta.fields:
        if name in ('sku', 'version', 'on_hand', 'tax_rate'):
            continue
        value = form[name].value()
        if isinstance(value, bool):
            if value: data[name] = 'on'
        elif value is not None:
            data[name] = value
    return data


@never_cache
@require_http_methods(['POST'])
@register_required('edit_items', 'edit_prices')
def special_order_item(request):
    """(matching QB's Add New Special Order Item): create a real inventory item from the sales order screen and
    hand it back as an item-search result, so the order gets an ordinary item line and Generate PO fills in the item,
    vendor, description and cost. Same rules as the Item List's Add Item (ItemForm + save_item_form: next item number,
    on-hand starts at 0 and duplicate names require confirmation). Requires item and catalog-price rights, and cost rights when supplying a cost."""
    import json
    from django.http import JsonResponse
    from sales.models import plain
    try:
        body = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    text = lambda key: str(body.get(key) or '').strip()
    if not text('vendor'):
        return JsonResponse({'error': 'Choose the vendor for this special order item.', 'field': 'vendor'}, status=400)
    data = new_item_form_data()
    data.update(name=text('name'), description=text('description'), department=text('department'),
                primary_vendor=text('vendor'), order_cost=text('cost'), price=text('price'))
    if text('cost') and not register_can(request, 'edit_costs'):
        return refused('edit_costs')
    if not data['price']:
        return JsonResponse({'error': 'Enter the price for this special order item.', 'field': 'price'}, status=400)
    from .models import Department
    department = Department.objects.filter(pk=data['department']).first() if data['department'].isdigit() and len(data['department']) <= 18 else None
    # The Add Item form's department choice sets the item's tax category and rate to the department's.
    defaults = new_item_tax()
    data.update(tax_category=department.category_name if department else defaults['tax_category'],
                tax_rate=rate_text(department.tax_rate) if department else rate_text(defaults['tax_rate']))
    blank = Item()
    original_stock, original_cost = blank.on_hand, blank.average_cost
    form = ItemForm(data, instance=blank)
    form.fields['primary_vendor'].queryset = form.fields['primary_vendor'].queryset.filter(active=True)
    form.fields['department'].queryset = form.fields['department'].queryset.filter(active=True)
    if not form.is_valid():
        labels = {'name': 'Item Name', 'description': 'Description', 'department': 'Department',
                  'primary_vendor': 'Vendor', 'order_cost': 'Cost', 'price': 'Price'}
        field, errors = next(iter(form.errors.items()))
        label = labels.get(field, '')
        return JsonResponse({'error': (label + ': ' if label else '') + ' '.join(errors),
                             'field': {'primary_vendor': 'vendor', 'order_cost': 'cost'}.get(field, field)}, status=400)
    if body.get('confirm_duplicate_name') is not True and (skus := same_name_items(form.cleaned_data.get('name'))):
        return JsonResponse({'duplicate': same_name_message(skus)}, status=409)
    try:
        item = save_item_form(form, None, 0, original_stock, original_cost)
    except IntegrityError:
        return JsonResponse({'error': 'That item number already exists. Try again.'}, status=409)
    return JsonResponse({'item': dict(id=item.pk, label=item.display_name, sku=item.sku, description=item.description,
                                      price=plain(item.price), vendor=item.vendor, on_hand=quantity_text(item.on_hand),
                                      available=quantity_text(item.on_hand),
                                      tax_rate=rate_text(item.tax_rate) if item.tax_rate is not None else None)})


@never_cache
@xframe_options_sameorigin
@require_http_methods(['GET', 'HEAD', 'POST'])
@register_required('edit_items')
def item_edit(request, pk=None):
    modal = request.GET.get('modal') == '1'
    return_to = item_return_url(request.POST.get('return_to', request.GET.get('return_to', '')))
    copied = None
    initial = {} if pk else {**new_item_tax(), 'on_hand': Decimal('0')}
    if not pk and request.GET.get('copy'):
        key = request.GET['copy']
        if not key.isdigit() or len(key) > 18:
            raise Http404
        copied = get_object_or_404(Item, pk=key)
        # Copy product details, but never its identity, stock or transaction history.
        excluded = {'sku', 'on_hand', 'version'}
        initial = {name: getattr(copied, name) for name in ItemForm.Meta.fields if name not in excluded}
        initial['on_hand'] = Decimal('0')
    with transaction.atomic():
        item = get_object_or_404(Item.objects.select_for_update(), pk=pk) if pk else Item()
        if not pk and not register_can(request, 'adjust_stock'):
            item.on_hand = Decimal('0')
        if copied: item.photo = copied.photo
        version = item.version
        original_stock, original_cost = item.on_hand, item.average_cost
        original_item_name = item.name if pk else ''
        original_name = (original_item_name or '').strip().lower() if pk else None
        same_name = None
        form = ItemForm(request.POST if request.method == 'POST' else None,
                        request.FILES if request.method == 'POST' else None, instance=item, initial=initial)
        denied = restrict_item_form(request, form)
        if denied is not None: return denied
        vendors_data = request.POST if request.method == 'POST' and 'vendors-TOTAL_FORMS' in request.POST else None
        vendor_initial = [dict(vendor=v.vendor_id,order_cost=v.order_cost,upc=v.upc,alu=v.alu) for v in copied.alternate_vendors.all()] if copied and request.method != 'POST' else None
        vendors = ItemVendorFormSet(vendors_data, instance=item, prefix='vendors', initial=vendor_initial)
        if not register_can(request, 'edit_costs') or not register_can(request, 'see_costs'):
            if vendors_data is not None and any(k.endswith('-order_cost') for k in vendors_data):
                return refused('edit_costs')
            for vendor_form in [*vendors.forms, vendors.empty_form]:
                vendor_form.fields.pop('order_cost', None)
        if request.method == 'POST':
            valid = form.is_valid()
            vendors_valid = vendors.is_valid() if vendors_data is not None else True
            if pk and form.cleaned_data.get('version') != version:
                form.add_error(None, 'Someone changed this item. Reload before saving.')
            elif valid and vendors_valid and request.POST.get('confirm_duplicate_name') != '1' \
                    and (form.cleaned_data.get('name') or '').strip().lower() != original_name \
                    and (skus := same_name_items(form.cleaned_data.get('name'), pk)):
                # a warning, not a block. A new name (or a changed one) that another active item already uses is
                # refused without the cashier's "Save anyway", so a stale page can't skip the question.
                same_name = same_name_message(skus)
                form.add_error('name', same_name)
            elif valid and vendors_valid:
                try:
                    item = save_item_form(form, pk, version, original_stock, original_cost,
                                          vendors if vendors_data is not None else None)
                    saved_params = {'saved': 1}
                    if not pk and request.POST.get('action') != 'save-next':
                        saved_params['added'] = 1   # the Item List says "Item added" (vs "Item saved")
                    if modal: saved_params['modal']=1
                    if return_to:
                        saved_params['return_to'] = return_to
                    if request.POST.get('action') == 'save-next':
                        return redirect('/purchasing/items/new/?' + urlencode(saved_params))
                    return redirect(f'/purchasing/items/{item.pk}/?' + urlencode(saved_params))
                except IntegrityError:
                    form.add_error(None, 'That item number or alternate vendor already exists. Review the highlighted fields.')
    context = dict(title='Edit Inventory Item' if pk else 'Add Inventory Item', record=item, form=form,
                   vendors=vendors, copied=copied, saved=request.GET.get('saved') == '1', added=request.GET.get('added') == '1', same_name=same_name, original_item_name=original_item_name,
                   readonly=False, modal=modal, return_to=return_to, department_taxes={str(d.pk):{'category':d.category_name,'rate':rate_text(d.tax_rate)} for d in form.fields['department'].queryset.select_related('tax_category')}, **field_groups(form))
    if pk:
        context.update(activity(item))
        context['po_rows'] = [row for row in open_orders(item) if row['kind'] == 'po']  # same list as Item Detail
        context['related_styles'] = Item.objects.filter(style=item.style).exclude(pk=pk)[:20] if item.style else []
        from accounts.admin_mode import is_on
        context['admin_mode_on'] = is_on(request)
        context['trash_entry'] = getattr(item, 'trash_entry', None) if not item.active else None
    return render(request, 'purchasing/item-edit.html', context, status=400 if request.method == 'POST' else 200)


CENT = Decimal('0.01')
MONEY_FIELDS = {'price', 'price_2', 'price_3', 'price_4', 'price_5', 'average_cost', 'order_cost'}
QUANTITY_FIELDS = {'on_hand', 'reorder_point'}
DETAIL_GROUPS = (
    ('Basic Info', [n for n in GROUPS['basic'] if n not in ('adjustment_note', 'on_hand')]),  # on hand is added after Avg. Unit Cost
    ('More Info', GROUPS['more']),
    ('Misc and Shipping', GROUPS['shipping']),
)


def detail_value(item, name):
    """Read-only display text for one Item field, as shown in the item list's details panel."""
    field = Item._meta.get_field(name)
    value = getattr(item, name)
    if field.choices and value not in (None, ''):
        value = getattr(item, f'get_{name}_display')()
    if value is None or value == '':
        return '—'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if name in MONEY_FIELDS:
        return f'${value.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}'  # whole cents, rounded half-up
    if name in QUANTITY_FIELDS:
        return quantity_text(value)  # whole numbers, no '.000'
    return str(value)


def open_orders(item):
    """Open purchase orders and customer (sales) orders that still have quantity outstanding for this item.

    Matches QB POS "Show: All Orders": one row per open document with its remaining quantity, oldest first.
    Imported QB POs and POs entered in this app are both listed (remaining = ordered - received).
    PO rows dated before ON_ORDER_FROM carry counted=False (shown greyed, "before 2026 - not counted") and sort
    after the counted rows; only counted rows add up to the item's On Order.
    """
    from .catalog_quantities import open_po_lines, counts_on_order
    rows = []
    for line in (open_po_lines().filter(item=item)
                 .select_related('document').order_by('document__created_at', 'document__pk', 'position')):
        doc = line.document
        # Expected = the PO's Preferred Pickup Date (ship_date; imported POs keep QB's Ship Date there).
        rows.append(dict(kind='po', type='Purchase Order', date=doc.created_at, number=doc.number,
                         details=doc.vendor_name, quantity=line.quantity - line.received, url=f'/purchasing/po/{doc.token}/',
                         expected=doc.ship_date, counted=counts_on_order(doc)))
    for line in (Line.objects.filter(item=item, document__kind='order', document__status='open')
                 .select_related('document').order_by('document__created_at', 'document__pk', 'position')):
        remaining = line.quantity - line.fulfilled  # same rule as activity()['customer_orders']
        if remaining <= 0:
            continue
        doc = line.document
        rows.append(dict(kind='order', type='Customer Order', date=doc.created_at, number=doc.number,
                         details=doc.customer_name, quantity=remaining, url=f'/pos/order/{doc.token}/', counted=True))
    rows.sort(key=lambda row: (not row['counted'], row['date']))
    return rows


@never_cache
@require_safe
def item_details(request, pk):
    """Read-only Inventory Item Detail fragment for the docked panel beside the Item List (QB POS "Show Details")."""
    item = get_object_or_404(Item.objects.select_related('department', 'primary_vendor').defer('photo'), pk=pk)
    labels = ItemForm(instance=item)
    data = activity(item)
    orders = open_orders(item)
    po_listed = sum((row['quantity'] for row in orders if row['kind'] == 'po' and row['counted']), Decimal('0'))
    available = data['available']  # On Hand - customer orders - held (activity)
    sections = []
    for title, names in DETAIL_GROUPS:
        rows = []
        for name in names:
            value = detail_value(item, name)
            if name == 'average_cost' and item.average_cost is None:
                value = item.average_cost_missing_label  # "Not received yet" for a new item, "Unverified" for an unknown count
            if name not in ('average_cost', 'order_cost') or register_can(request, 'see_costs'):
                rows.append(dict(label=labels[name].label if name in labels.fields else name, value=value))
            if name == 'average_cost':
                rows.append(dict(label='On Hand Qty', value=detail_value(item, 'on_hand') if item.on_hand is not None else 'Unverified',
                                 button='available', button_label='Available'))
                rows.append(dict(label='Held Qty', value=quantity_text(data['held'])))
                rows.append(dict(label='Available Qty', value=quantity_text(available) if available is not None else 'Unverified',
                                 button='orders', button_label='Orders'))
                # the open POs are listed right here, no extra click (QB's Orders button needed two).
                rows.append(dict(label='On Order', value=quantity_text(data['on_order'] or Decimal('0')), po_table=True))
        sections.append((title, rows))
    pricing = [(labels[name].label, detail_value(item, name)) for name in GROUPS['pricing'] if getattr(item, name) is not None]
    has_photo = Item.objects.filter(pk=pk).exclude(photo=None).exclude(photo=b'').exists()
    context = dict(record=item, sections=sections, pricing=pricing, has_photo=has_photo, orders=orders,
                   po_rows=[row for row in orders if row['kind'] == 'po'], po_listed=po_listed,
                   has_uncounted=any(not row['counted'] for row in orders), **data)
    context['po_mismatch'] = po_listed != data['on_order']
    context['history'] = context['history'][:10]
    return render(request, 'purchasing/item-details-panel.html', context)


@never_cache
@require_safe
def item_photo(request, pk):
    record = get_object_or_404(Item.objects.only('photo'), pk=pk)
    if not record.photo:
        raise Http404
    response = HttpResponse(bytes(record.photo), content_type='image/jpeg')
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@never_cache
@require_safe
def item_tag(request, pk):
    """Item Price Tag: the Zebra tag with editable text and a live preview, plus the Letter tag."""
    record = get_object_or_404(Item, pk=pk)
    from reports.direct_print import letter_printers
    from tags.views import item_tag_context
    session = getattr(request, 'session', None)
    context = item_tag_context(request, record, f'/purchasing/items/{record.pk}/tag/print/')
    context.update(record=record, title='Item Price Tag', printers=letter_printers(),
                   tag_printer=session.get('tag_printer', '') if session is not None else '')
    return render(request, 'purchasing/item-tag.html', context)


@never_cache
@require_http_methods(['POST'])
def item_tag_print(request, pk):
    from django.http import JsonResponse
    from sales.models import Printer
    from sales.letter_printing import tag_pdf
    from sales.printing import PrintError
    from reports.direct_print import print_report

    record = get_object_or_404(Item, pk=pk)
    if not record.print_tags:
        return JsonResponse({'error': 'Print Tags is off for this item. Enable it in Edit Item before printing.'}, status=400)
    key = request.POST.get('printer', '')
    printer = Printer.objects.filter(pk=key, active=True, kind__in=['label', 'letter']).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose an active tag or Letter printer.'}, status=400)
    copies = request.POST.get('copies', '1')
    if not copies.isdigit() or len(copies) > 2 or not 1 <= int(copies) <= 10:
        return JsonResponse({'error': 'Choose 1 to 10 copies.'}, status=400)
    copies = int(copies)
    if printer.kind == 'label':
        # the Zebra tag prints the text shown in the editor (fields left out fall back to the Item List).
        from sales.label_printing import prefill
        from tags.views import print_label_tag
        return print_label_tag(request, prefill(record))
    try:
        print_report(printer, tag_pdf(record, copies), f'Item tag {record.item_number}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    session = getattr(request, 'session', None)
    if session is not None:
        session['tag_printer'] = str(printer.pk)
    return JsonResponse({'message': f'Sent {copies} tag{"s" if copies != 1 else ""} to {printer.name}.'})
