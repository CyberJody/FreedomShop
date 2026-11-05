# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, F, OuterRef, Subquery
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe
from sales.models import Item, Document as SalesDocument, plain
from sales.services import WorkflowError
from .models import Vendor, Department, PurchaseDocument, PurchaseLine
from .forms import VendorForm, DepartmentForm, ItemForm, HeaderForm, LineSet, POLineSet
from .services import save_purchase
from .item_views import item_edit, search_items
from config.lists import sorted_list
from .procurement import procurement_capacity
from .item_scrolling import item_rows, item_scroll_config
from config.list_scroll import wants_rows, scroll_rows, scroll_config, link, fmt_date, qty, money

CATALOG={'items':(Item,ItemForm,'Item List','Item',['sku','description']), 'vendors':(Vendor,VendorForm,'Vendor List','Vendor',['code','company','contact','phone','city']), 'departments':(Department,DepartmentForm,'Department List','Department',['code','name'])}


def page_context(request, queryset):
    params=request.GET.copy();params.pop('page',None)
    return {'page':Paginator(queryset,50).get_page(request.GET.get('page')),'params':params.urlencode()}


@never_cache
@require_safe
def catalog(request, section):
    model,_,title,singular,fields=CATALOG[section]
    query=request.GET.get('q','').strip()[:100];status=request.GET.get('status','active')
    qs=model.objects.select_related('tax_category') if section=='departments' else model.objects.all()
    if status in {'active','inactive'}:qs=qs.filter(active=status=='active')
    elif status=='unused' and section=='items':
        from sales.trash import never_used  # "Never used": no sales, order, held-receipt, PO/voucher or tag history
        qs=never_used(qs.filter(active=True))
    else:status='all'
    if query:
        if section == 'items':
            qs = search_items(qs, query)
        else:
            match=Q()
            for field in fields:match |= Q(**{field+'__icontains':query})
            qs=qs.filter(match).distinct()
    if section=='items':
        from .catalog_quantities import catalog_quantities
        from sales.holds import annotate_held  # Held column = what app-made held receipts reserve
        qs=annotate_held(catalog_quantities(qs.defer('photo', 'comments').select_related('department','primary_vendor')))
    fields_by_section={'items':['name','description','on_hand','held','price','on_order','last_received','size','primary_vendor__company','department__name','order_cost','average_cost','upc','alu','tax_rate','active','sku'], 'vendors':['code','company','contact','phone','street','city','state','postal_code','email','active'], 'departments':['name','code','margin','tax_category__name','markup','active']}
    qs,sorting=sorted_list(request,qs,fields_by_section[section],{'items':'name','vendors':'company','departments':'name'}[section])
    if section=='items' and request.GET.get('format')=='rows':
        return item_rows(request,qs,'inventory')
    if section!='items' and wants_rows(request):
        return scroll_rows(request,qs,lambda record:catalog_row(record,section))
    paging = page_context(request, qs)
    scroll = item_scroll_config(request, qs, paging['page'], 'inventory', query, status) if section == 'items' else None
    list_scroll = None if section == 'items' else scroll_config(request, paging['page'], len(fields_by_section[section]))
    return render(request,'purchasing/catalog.html',dict(title=title,singular=singular,section=section,query=query,status=status,item_scroll_config=scroll,list_scroll_config=list_scroll,scroll_view=request.GET.get('view'),**sorting,**paging))


def catalog_row(record, section):
    href = f'/purchasing/{section}/{record.pk}/'
    status = 'Active' if record.active else 'Inactive'
    if section == 'vendors':
        cells = [[link(record.code, href)], [link(record.company, href)], record.contact, record.phone, record.street, record.city, record.state, record.postal_code, record.email, status]
        name = record.company or record.code
        return dict(id=record.pk, name=name, href=href, cells=cells, details=record.details_text)  # Contact column + Show Details extras
    else:
        cells = [[link(record.name, href)], record.code, str(record.margin), record.tax_label, str(record.markup), status]  # tax code with its rate
        name = record.name
    return dict(id=record.pk, name=name, href=href, cells=cells)


@never_cache
@require_http_methods(['GET','HEAD','POST'])
def catalog_edit(request, section, pk=None):
    if section == 'items':
        return item_edit(request, pk)
    model,form_class,title,singular,_=CATALOG[section]
    copied = None
    initial = {}
    if not pk and request.GET.get('copy'):
        key=request.GET['copy']
        if not key.isdigit() or len(key)>18: raise Http404
        copied=get_object_or_404(model,pk=key)
        initial={name:getattr(copied,name) for name in form_class().fields if name not in {'code','version','active'}}
    with transaction.atomic():
        instance=get_object_or_404(model.objects.select_for_update(),pk=pk) if pk else model()
        version=instance.version
        form=form_class(request.POST if request.method=='POST' else None,instance=instance,initial=initial)
        if request.method=='POST' and form.is_valid():
            if pk and form.cleaned_data.get('version')!=version:
                form.add_error(None,'Someone changed this record. Reload before saving.')
            else:
                try:
                    with transaction.atomic():
                        record=form.save(commit=False);record.version=version+1 if pk else 1
                        if section=='items':
                            record.vendor=record.primary_vendor.code if record.primary_vendor else ''
                        record.save()
                    return redirect(f'/purchasing/{section}/{record.pk}/?saved=1' + ('' if pk else '&added=1'))
                except IntegrityError:
                    form.add_error(None,'That code already exists. Choose a unique code.')
    # "Vendor added." after a new vendor, "Vendor saved." after an edit (it said "Edit Vendor saved." for both).
    saved=request.GET.get('saved')=='1'
    saved_note=(singular+(' added.' if request.GET.get('added')=='1' else ' saved.')) if saved else ''
    return render(request,'purchasing/catalog-edit.html',dict(title=('Edit ' if pk else 'Add ')+singular,form=form,section=section,record=instance,copied=copied,saved=saved,saved_note=saved_note,vendor_terms={str(v.pk):v.terms for v in Vendor.objects.all()}),status=400 if request.method=='POST' else 200)


@never_cache
@require_safe
def item_lookup(request):
    query = request.GET.get('q', '').strip()[:100]
    vendor = request.GET.get('vendor', '')
    if vendor and (not vendor.isdigit() or len(vendor) > 18):
        return JsonResponse({'results': []})
    items = search_items(Item.objects.filter(active=True), query).prefetch_related('alternate_vendors')[:20] if query else []
    results = []
    for item in items:
        cost = item.order_cost
        if vendor and str(item.primary_vendor_id) != vendor:
            alternate = next((row for row in item.alternate_vendors.all() if str(row.vendor_id) == vendor), None)
            cost = alternate.order_cost if alternate else None
        results.append(dict(id=item.pk, label=item.display_name, description=item.description, cost=str(cost.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if cost is not None else ''))
    return JsonResponse({'results': results})


def row_initial(line, source=None):
    # A receiving voucher's line never carries the customer-order link itself (save_purchase refuses sales_line on a
    # voucher); it reaches the order through its PO line (source_line), so receiving a PO made by Generate PO works.
    return dict(line_id=None if source else line.pk,item=line.item_id,item_name=line.item_name or line.sku,item_description=line.description,quantity=line.due if source=='po' else line.quantity,cost=plain(line.cost),source_line=line.pk if source=='po' else line.source_line_id,sales_line=None if source=='po' else line.sales_line_id)


@never_cache
@require_http_methods(['GET','HEAD','POST'])
def editor(request, kind, token=None):
    doc=get_object_or_404(PurchaseDocument,token=token,kind=kind) if token else None
    po=doc.source_po if doc else None;order=doc.source_order if doc else None
    orders=list(doc.customer_orders) if doc and kind=='po' else (list(po.customer_orders) if po else ([order] if order else []))
    if doc and kind=='po' and doc.imported:
        # a QB-era PO shows the open customer order of each special-order piece on it (QB never linked them).
        from .order_coverage import orders_for_purchase_lines
        for matched in orders_for_purchase_lines(doc.lines.filter(sales_line__isnull=True,item__isnull=False).select_related('item__department')).values():
            orders+=[o for o in matched if o not in orders]
    if not doc:
        try:
            if request.GET.get('po'):
                po=get_object_or_404(PurchaseDocument,token=uuid.UUID(request.GET['po']),kind='po')
                orders=list(po.customer_orders)
            tokens=request.GET.getlist('order')
            if tokens:
                if len(tokens)>100: raise ValueError
                orders=[get_object_or_404(SalesDocument,token=uuid.UUID(t),kind='order') for t in dict.fromkeys(tokens)]
                order=orders[0] if len(orders)==1 else None
        except ValueError:
            return JsonResponse({'error':'Invalid source document.'},status=400)
    readonly=bool(doc and (doc.imported or (kind=='po' and doc.status=='cancelled')  # a cancelled PO opens read-only
                           or (kind=='voucher' and doc.status in ('posted','cancelled'))))  # so do received and cancelled vouchers
    can_reverse=bool(doc and kind=='voucher' and not doc.imported and doc.status=='posted')  # Reverse Voucher
    # Receive Items (Ctrl+F7) — shown on an open PO with a vendor; greyed out like QB when nothing is left to receive.
    show_receive=bool(doc and kind=='po' and doc.status=='open' and doc.vendor_id)
    can_receive=bool(show_receive and doc.due>0)
    initial=dict(token=doc.token if doc else uuid.uuid4(),version=doc.version if doc else 0,status='open')
    rows=[];left_off=[];vendor_choices=[]
    if doc:
        for key in ['vendor','status','notes','terms','ship_date','alternate_pickup_date','payment_due','invoice_number']:
            initial[key]=getattr(doc,key)
        rows=[row_initial(line) for line in doc.lines.select_related('item')]
    elif po:
        initial.update(vendor=po.vendor_id,terms=po.terms,notes=po.notes)
        # nothing is received on a cancelled PO (the save refuses it too).
        rows=[] if po.status=='cancelled' else [row_initial(line,'po') for line in po.lines.all() if line.due>0]
    elif orders:
        from sales.models import Line as SalesLine, quantity_text
        from .order_coverage import coverage
        # lines with no linked PO line are matched to POs by item (QB never linked its PO lines to orders).
        order_lines=list(SalesLine.objects.filter(document__in=orders).select_related('item__department','document'))
        covered=coverage(order_lines)
        candidates=[]
        for line in order_lines:
            remaining=covered[line.pk].remaining
            if covered[line.pk].reason:
                name=(line.item.display_name if line.item else '') or line.item_name or line.sku or line.description
                note=f'{covered[line.pk].reason}' + (f'; {quantity_text(remaining)} offered below' if remaining>0 else '')
                left_off.append(dict(order=line.document,name=name,note=note))
            if remaining<=0:continue
            candidates.append((line,remaining))
        # a PO goes to one vendor, so only the lines whose item's (primary) vendor is the PO's vendor are offered.
        # With several vendors on the chosen orders, staff pick one (vendor_choices); the rest wait for their own PO.
        counts={}
        for line,_ in candidates:
            if line.item and line.item.primary_vendor_id: counts[line.item.primary_vendor_id]=counts.get(line.item.primary_vendor_id,0)+1
        chosen=None
        if request.GET.get('vendor'):
            key=request.GET['vendor']
            if not key.isdigit() or len(key)>18: raise Http404
            chosen=get_object_or_404(Vendor,pk=key)
        elif len(counts)==1:
            chosen=Vendor.objects.get(pk=next(iter(counts)))
        if chosen: initial['vendor']=chosen.pk
        for line,remaining in candidates:
            if line.item and not line.item.primary_vendor_id:
                left_off.append(dict(order=line.document,name=line.item.display_name,note='no vendor on this item; set its vendor in the Item List, then make the PO'))
                continue
            if line.item and (chosen is None or line.item.primary_vendor_id!=chosen.pk): continue
            if not line.item and chosen is None and counts: continue  # a line with no item waits until a vendor is chosen
            rows.append(dict(item=line.item_id,item_name=line.item.display_name if line.item else '',item_description=line.item.description if line.item else '',quantity=remaining,cost=line.item.order_cost if line.item else None,sales_line=line.pk))
        if len(counts)>1 or (chosen and counts and chosen.pk not in counts):
            for vendor in Vendor.objects.filter(pk__in=counts).order_by('company','pk'):
                params=request.GET.copy();params['vendor']=str(vendor.pk)
                vendor_choices.append(dict(vendor=vendor,count=counts[vendor.pk],url='?'+params.urlencode(),current=bool(chosen and chosen.pk==vendor.pk)))
        initial['notes']='\n'.join(f'For {o.numbered_label} · {o.customer_name}\n{o.notes}' for o in orders)[:5000]
    else:
        ids=request.GET.getlist('item')
        if any(not x.isdigit() or len(x)>18 for x in ids) or len(ids)>100: raise Http404
        selected=Item.objects.filter(pk__in=ids,active=True)
        rows=[dict(item=i.pk,item_name=i.display_name,item_description=i.description,quantity=1,cost=i.order_cost) for i in selected] or [{}]
        vendors={i.primary_vendor_id for i in selected if i.primary_vendor_id}
        if len(vendors)==1: initial['vendor']=vendors.pop()
        if request.GET.get('vendor'):
            key = request.GET['vendor']
            if not key.isdigit() or len(key) > 18:
                raise Http404
            vendor = get_object_or_404(Vendor, pk=key, active=True)
            initial.update(vendor=vendor.pk, terms=vendor.terms)
    header=HeaderForm(request.POST if request.method=='POST' else None,initial=initial)
    if kind=='voucher':header.fields.pop('status');header.fields.pop('alternate_pickup_date')
    formset=(POLineSet if kind=='po' else LineSet)(request.POST if request.method=='POST' else None,initial=rows,prefix='lines')
    error=f'{po.number} was cancelled. Nothing can be received on it.' if po and not doc and po.status=='cancelled' and request.method!='POST' else ''
    if request.method=='POST':
        action=request.POST.get('action')
        try:
            if action=='cancel' and doc:
                version=int(request.POST.get('version','-1'))
                saved=save_purchase({'token':doc.token,'version':version},[],kind,action,po,order)
            elif header.is_valid() and formset.is_valid():
                if doc and header.cleaned_data['token']!=doc.token:
                    raise WorkflowError('Document does not match this page.')
                saved=save_purchase(header.cleaned_data,formset.cleaned_data,kind,action,po,order)
            else:
                saved=None
            if saved:
                target=f'/purchasing/{kind}/{saved.token}/?saved=1'
                if request.POST.get('print')=='1':target+='&print=1'
                return redirect(target)
        except (WorkflowError,ValueError) as exc:error=str(exc)
        except IntegrityError:error='This document was saved by another request. Reload before continuing.'
    source_rows=[l for o in orders for l in o.lines.all()]
    source_names={l.pk:f'{l.document.numbered_label} · {l.document.customer_name} · {l.sku} · {l.description}' for l in source_rows}
    po_source_rows={l.pk:l for l in po.lines.select_related('sales_line__document')} if po else {}
    saved_rows={l.pk:l for l in doc.lines.all()} if doc else {}
    for form in formset:
        try:form.source_label=source_names.get(int(form['sales_line'].value() or 0),'')
        except (TypeError,ValueError):form.source_label=''
        try:
            original=po_source_rows.get(int(form['source_line'].value() or 0))
            saved=saved_rows.get(int(form['line_id'].value() or 0))
        except (TypeError,ValueError): original=saved=None
        if original and original.sales_line:
            source=original.sales_line
            form.source_label=f'{source.document.numbered_label} · {source.document.customer_name} · {source.description}'
        form.received=saved.received if saved else 0
        form.due=saved.due if saved else form['quantity'].value()
    trucks=[]
    if doc:
        # "Received by truck: TRUCK-12 (10/25)" on a PO; "From truck" on a voucher Receive Items made from a truck.
        from .truck import po_trucks
        from .models import TruckSession
        trucks=po_trucks(doc) if kind=='po' else list(TruckSession.objects.filter(scans__voucher=doc).distinct())
    return render(request,'purchasing/editor.html',dict(title='Purchase Order' if kind=='po' else 'Receiving Voucher',trucks=trucks,kind=kind,doc=doc,header=header,formset=formset,readonly=readonly,can_reverse=can_reverse,error=error,show_receive=show_receive,can_receive=can_receive,source_po=po,source_order=order,source_orders=orders,left_off=left_off,vendor_choices=vendor_choices,saved=request.GET.get('saved')=='1',vendor_terms={str(v.pk):v.terms for v in Vendor.objects.all()},auto_print=request.GET.get('print')=='1' and doc is not None,**print_context(request,kind)),status=400 if request.method=='POST' else 200)


@never_cache
@require_safe
def document_list(request,section):
    kind,status,title={'orders':('po','open','Purchase Order List'),'history':('voucher','posted','Receiving History'),'held':('voucher','held','Held Vouchers')}[section]
    selected=request.GET.get('status',status)
    qs=PurchaseDocument.objects.filter(kind=kind)
    vendor = None
    if request.GET.get('vendor'):
        key = request.GET['vendor']
        if not key.isdigit() or len(key) > 18:
            raise Http404
        vendor = get_object_or_404(Vendor, pk=key)
        qs = qs.filter(vendor=vendor)
    if section=='orders' and selected in {'open','pending','closed','cancelled'}:qs=qs.filter(status=selected)
    elif section=='orders' and selected=='all':pass
    elif section=='history' and selected in {'posted','cancelled'}: qs=qs.filter(status=selected)
    elif section=='history' and selected=='all': qs=qs.exclude(status='held')
    else:selected=status;qs=qs.filter(status=status)
    query=request.GET.get('q','').strip()[:100]
    if query:
        match=Q(vendor_name__icontains=query)|Q(invoice_number__icontains=query)|Q(source_order__customer_name__icontains=query)|Q(lines__sales_line__document__customer_name__icontains=query)
        number=query.upper().removeprefix('PO-').removeprefix('RV-')
        if number.isdigit() and len(number)<=18:match|=Q(pk=int(number))|Q(source_number=number)
        qs=qs.filter(match).distinct()
    if request.GET.get('order'):
        try: order_token=uuid.UUID(request.GET['order'])
        except ValueError: raise Http404
        # "View POs" also lists POs holding the order's special-order pieces without a link (QB-era POs).
        from .order_coverage import matched_purchase_orders
        order_doc=SalesDocument.objects.filter(token=order_token,kind='order').first()
        matched=matched_purchase_orders(order_doc).values('pk') if order_doc else PurchaseDocument.objects.none().values('pk')
        qs=qs.filter(Q(source_order__token=order_token)|Q(lines__sales_line__document__token=order_token)|Q(pk__in=matched)).distinct()
    period=request.GET.get('period','30' if section=='history' else 'all')
    if period in {'7','30','90'}:
        datefield='posted_at' if section=='history' else 'created_at'
        qs=qs.filter(**{datefield+'__gte':timezone.now()-timedelta(days=int(period))})
    else:period='all'
    date_field='posted_at' if section=='history' else 'created_at'
    qs=qs.select_related('source_order').prefetch_related('lines')
    fields=['pk',date_field,'status','vendor_name','']+(['',''] if section=='orders' else [])+['total','invoice_number','']
    qs,sorting=sorted_list(request,qs,fields,'-'+date_field)
    if wants_rows(request):
        return scroll_rows(request,qs,lambda doc:purchase_row(doc,section),prepare=linked_orders if section=='orders' else None)
    paging=page_context(request,qs)
    if section=='orders': linked_orders(paging['page'].object_list)
    return render(request,'purchasing/list.html',dict(title=title,section=section,status=selected,query=query,period=period,vendor=vendor,**sorting,**paging,list_scroll_config=scroll_config(request,paging['page'],len(fields))))


def linked_orders(docs):
    """Resolve PurchaseDocument.customer_orders for a batch with two indexed queries instead of one OR-join scan per row."""
    docs=list(docs)
    if not docs: return docs
    by_pk={d.pk:d for d in docs}
    orders={}
    pairs=PurchaseLine.objects.filter(document__in=by_pk,sales_line__isnull=False).values_list('document_id','sales_line__document_id')
    wanted={oid for _,oid in pairs}|{d.source_order_id for d in docs if d.source_order_id}
    if wanted: orders={o.pk:o for o in SalesDocument.objects.filter(pk__in=wanted)}
    found={pk:[] for pk in by_pk}
    for doc_id,order_id in pairs:
        if order_id in orders and orders[order_id] not in found[doc_id]: found[doc_id].append(orders[order_id])
    for d in docs:
        if d.source_order_id in orders and orders[d.source_order_id] not in found[d.pk]: found[d.pk].append(orders[d.source_order_id])
    # a PO line holding a special-order piece with no link (QB-era POs) shows that piece's open customer order.
    from .order_coverage import orders_for_purchase_lines
    unlinked=PurchaseLine.objects.filter(document__in=by_pk,sales_line__isnull=True,item__isnull=False).select_related('item__department')
    for doc_id,matched in orders_for_purchase_lines(unlinked).items():
        for order in matched:
            if order not in found[doc_id]: found[doc_id].append(order)
    for d in docs:
        d.customer_orders=found[d.pk]
    return docs


def purchase_row(doc, section):
    href = f'/purchasing/{doc.kind}/{doc.token}/'
    cells = [[link(doc.number, href)], fmt_date(doc.posted_at if section == 'history' else doc.created_at), doc.get_status_display(), doc.vendor_name, qty(doc.ordered)]
    if section == 'orders':
        cells += [qty(doc.due), qty(doc.received)]
    cells.append(money(doc.total))
    if section == 'orders':
        orders = [link(f'{o.numbered_label} · {o.customer_name}', f'/pos/order/{o.token}/') for o in doc.customer_orders]
        linked = []
        for index, item in enumerate(orders):
            if index: linked.append(' · ')
            linked.append(item)
        cells.append(linked or '—')
    else:
        cells.append(doc.invoice_number or '—')
    actions = [link('Unhold' if section == 'held' else 'Show Details', href)]
    if section == 'orders' and doc.status == 'open':
        actions += [' · ', link('Receive', f'/purchasing/voucher/new/?po={doc.token}')]
    cells.append(actions)
    return dict(id=doc.pk, token=str(doc.token), name=f'{doc.number} · {doc.vendor_name}', href=href, cells=cells)


# ---- Printing: Letter PDF + Direct Print to the Brother printers, like the reports ---------------------------

PRINTER_SESSION_KEY = 'report_printer'  # shared with reports, so a shop's chosen Letter printer is remembered
PO_PRINTER_SESSION_KEY = 'po_printer'   # POs remember their own printer (set tags and reports keep theirs)
PO_TRAY = 'tray-2'


def po_default_printer(printers):
    """POs print from Tray 2 of the office Brother. That tray is its own Letter printer row (same host,
    Paper tray = Tray 2). Prefer a Tray 2 row on the color office printer's host, else any Tray 2 row."""
    trays = [p for p in printers if p.media_source == PO_TRAY]
    office_hosts = {p.host for p in printers if p.color}
    preferred = [p for p in trays if p.host in office_hosts]
    choice = (preferred or trays or [None])[0]
    return str(choice.pk) if choice else ''


def print_context(request, kind=None):
    from reports.direct_print import letter_printers
    printers = list(letter_printers())
    session = getattr(request, 'session', None)
    keys = [str(p.pk) for p in printers]
    extra = dict(label_selected_printer=label_print_context(request, printers)) if kind == 'po' else {}  # labels
    if kind == 'po':
        chosen = str(session.get(PO_PRINTER_SESSION_KEY, '')) if session is not None else ''
        if chosen not in keys:
            chosen = po_default_printer(printers)
        if chosen in keys:
            return dict(printers=printers, selected_printer=chosen, **extra)
    chosen = str(session.get(PRINTER_SESSION_KEY, '')) if session is not None else ''
    return dict(printers=printers, selected_printer=chosen if chosen in keys else (keys[0] if keys else ''), **extra)


def _printable(kind, token):
    return get_object_or_404(PurchaseDocument.objects.select_related('vendor', 'source_po'), token=token, kind=kind)


@never_cache
@require_safe
def document_pdf(request, kind, token):
    """The printable PO / receiving voucher as a PDF. Opens in the browser; ?download=1 saves it."""
    from django.http import HttpResponse
    from .print_exports import pdf_bytes
    doc = _printable(kind, token)
    response = HttpResponse(pdf_bytes(doc), content_type='application/pdf')
    disposition = 'attachment' if request.GET.get('download') == '1' else 'inline'
    response['Content-Disposition'] = f'{disposition}; filename="{doc.number}.pdf"'
    return response


@never_cache
@require_http_methods(['POST'])
def document_print(request, kind, token):
    """Render the PDF and send it to the chosen Letter (Brother) printer."""
    from reports.direct_print import letter_printers, print_report
    from sales.printing import PrintError
    from .print_exports import pdf_bytes
    doc = _printable(kind, token)
    key = request.POST.get('printer', '')
    printer = letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    title = 'Purchase Order' if kind == 'po' else 'Receiving Voucher'
    try:
        message = print_report(printer, pdf_bytes(doc), f'{title} {doc.number}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PO_PRINTER_SESSION_KEY if kind == 'po' else PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


# ---- PO item labels: one QR label per piece ordered, SheetLabels SL548 (15 per Letter sheet) -----------------

LABEL_PRINTER_SESSION_KEY = 'label_printer'   # labels remember their own printer (POs keep Tray 2)
LABEL_TRAY = 'tray-1'


def label_default_printer(printers):
    """Where PO item labels start: the printer row named in settings.PIECE_LABEL_PRINTER, else Tray 1 of the color
    office Brother (the label sheets go in Tray 1 for now), else any color Letter printer, else the first one."""
    from django.conf import settings
    named = (getattr(settings, 'PIECE_LABEL_PRINTER', '') or '').strip()
    by_name = [p for p in printers if named and p.name == named]
    color_hosts = {p.host for p in printers if p.color}
    tray_one = [p for p in printers if p.media_source == LABEL_TRAY and p.host in color_hosts]
    tray_one.sort(key=lambda p: not p.color)
    choice = (by_name or tray_one or [p for p in printers if p.color] or printers or [None])[0]
    return str(choice.pk) if choice else ''


def label_print_context(request, printers):
    session = getattr(request, 'session', None)
    keys = [str(p.pk) for p in printers]
    chosen = str(session.get(LABEL_PRINTER_SESSION_KEY, '')) if session is not None else ''
    if chosen not in keys:
        chosen = label_default_printer(printers)
    return chosen


def _copies(value):
    value = (value or '1').strip()
    return int(value) if value.isdigit() and len(value) <= 2 and 1 <= int(value) <= 5 else None


@never_cache
@require_http_methods(['POST'])
def po_labels_pdf(request, token):
    """Preview PDF of the PO's item labels (opens in a new tab). Makes the piece codes on first use, like Print Labels,
    so the preview and the printed sheet carry the same codes. outlines=1 adds the label outlines for a plain-paper
    alignment check."""
    from django.http import HttpResponse
    from .piece_labels import LabelError, labels_for_print, pdf_bytes
    doc = PurchaseDocument.objects.filter(token=token, kind='po').first()
    if doc is None:
        return HttpResponse('Save the purchase order first.', status=404, content_type='text/plain; charset=utf-8')
    copies = _copies(request.POST.get('copies'))
    if copies is None:
        return HttpResponse('Copies must be 1 to 5.', status=400, content_type='text/plain; charset=utf-8')
    try:
        labels = labels_for_print(doc, copies)
    except LabelError as exc:
        return HttpResponse(str(exc), status=400, content_type='text/plain; charset=utf-8')
    response = HttpResponse(pdf_bytes(labels, copies, outlines=request.POST.get('outlines') == '1',
                                      title=f'Item Labels {doc.number}'), content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{doc.number}-labels.pdf"'
    return response


@never_cache
@require_http_methods(['POST'])
def po_labels_print(request, token):
    """Send the PO's item labels (copies of the whole set) to the chosen Letter printer."""
    from reports.direct_print import letter_printers, print_report
    from sales.printing import PrintError
    from .piece_labels import LabelError, labels_for_print, pdf_bytes
    doc = PurchaseDocument.objects.filter(token=token, kind='po').first()
    if doc is None:
        return JsonResponse({'error': 'Save the purchase order first.'}, status=404)
    key = request.POST.get('printer', '')
    printer = letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    copies = _copies(request.POST.get('copies'))
    if copies is None:
        return JsonResponse({'error': 'Copies must be 1 to 5.'}, status=400)
    try:
        labels = labels_for_print(doc, copies)
        message = print_report(printer, pdf_bytes(labels, copies, title=f'Item Labels {doc.number}'),
                               f'Item Labels {doc.number}')
    except LabelError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[LABEL_PRINTER_SESSION_KEY] = str(printer.pk)
    count = len(labels) * copies
    return JsonResponse({'message': f'{count} label{"s" if count != 1 else ""}: {message}'})


@never_cache
@require_safe
def label_alignment_pdf(request):
    """SL548 alignment test: one sheet with only the label outlines, for a plain-paper check against a label sheet."""
    from django.http import HttpResponse
    from .piece_labels import alignment_pdf
    response = HttpResponse(alignment_pdf(), content_type='application/pdf')
    response['Content-Disposition'] = 'inline; filename="SL548-alignment-test.pdf"'
    return response
