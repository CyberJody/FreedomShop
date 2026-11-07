# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.permissions import register_can, register_required, refused
import json
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe, require_http_methods


from . import detail
from .models import Customer
from .forms import CustomerForm, CustomerAddressFormSet


def list_return_url(value):
    """Only carry list navigation state, never an external redirect destination."""
    try:
        parts = urlsplit(value[:2000])
    except ValueError:
        return ''
    if parts.scheme or parts.netloc or parts.path != '/customers/':
        return ''
    params = {key: val for key, val in parse_qsl(parts.query)
              if key in {'q', 'status', 'sort', 'page', 'at', 'customer', 'view'}}
    return '/customers/?' + urlencode(params)


def save_local_edit(record, version, fields):
    """Customer Edit's save of an existing customer: next version, stamped as a local edit, only `fields` written
    (the original source evidence and import timestamp stay unchanged). Also used by the register's contact prompt."""
    record.version = version + 1
    record.locally_edited_at = timezone.now()
    record.save(update_fields=list(dict.fromkeys([*fields, 'version', 'locally_edited_at'])))


CONTACT_LIMITS = {'phone': 100, 'street': 2000, 'city': 200, 'state': 100, 'postal_code': 100}


@never_cache
@require_http_methods(['POST'])
def customer_contact(request, pk):
    """the register's \"missing phone / address\" prompt. Fills only fields that are blank on the customer,
    through Customer Edit's save (version bump + local-edit stamp). Skip never calls this."""
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    values = {}
    for field, limit in CONTACT_LIMITS.items():
        value = data.get(field, '')
        if not isinstance(value, str):
            return JsonResponse({'error': 'Invalid request.'}, status=400)
        value = value.strip()
        if len(value) > limit:
            return JsonResponse({'error': f'{field.replace("_", " ").title()} is too long.'}, status=400)
        if value:
            values[field] = value
    if 'phone' in values and len([ch for ch in values['phone'] if ch.isdigit()]) < Customer.MIN_PHONE_DIGITS:
        return JsonResponse({'error': 'Enter a phone number with at least 7 digits.'}, status=400)
    address = {k: v for k, v in values.items() if k != 'phone'}
    if address and not (address.get('street') and address.get('city')):
        return JsonResponse({'error': 'Enter the street and city, or Skip.'}, status=400)
    if not values:
        return JsonResponse({'error': 'Enter a phone number or address, or Skip.'}, status=400)
    with transaction.atomic():
        record = get_object_or_404(Customer.objects.select_for_update(), pk=pk)
        if str(data.get('version')) != str(record.version):
            return JsonResponse({'error': 'Someone changed this customer. Search for the customer again.'}, status=409)
        # Never overwrite what is on file: the prompt only asks for missing parts.
        if 'phone' in values and record.phone_numbers():
            values.pop('phone')
        if address and record.has_address:
            for field in address:
                values.pop(field)
        if not values:
            return JsonResponse({'error': 'This customer already has that information.'}, status=409)
        for field, value in values.items():
            setattr(record, field, value)
        save_local_edit(record, record.version, list(values))
    from sales.views import customer_result
    return JsonResponse({'ok': True, 'customer': customer_result(record)})


@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def customer_edit(request, pk=None):
    return_to = list_return_url(request.POST.get('return_to', request.GET.get('return_to', '')))
    copied = None
    initial = {}
    if not pk and request.GET.get('copy'):
        source_pk = request.GET['copy']
        if not source_pk.isdigit() or len(source_pk) > 18:
            raise Http404
        copied = get_object_or_404(Customer, pk=source_pk)
        initial = {field: getattr(copied, field) for field in CustomerForm.Meta.fields
                   if field not in {'customer_number', 'version', 'active', 'charge_account', 'account_limit', 'use_with_quickbooks'}}
    with transaction.atomic():
        record = get_object_or_404(Customer.objects.select_for_update(), pk=pk) if pk else Customer()
        version = record.version
        form = CustomerForm(request.POST if request.method == 'POST' else None,
                            instance=record, initial=initial)
        if not register_can(request, 'manage_charge_accounts'):
            if request.method == 'POST' and any(k in request.POST for k in ('charge_account', 'account_limit')):
                return refused('manage_charge_accounts')
            if not pk: record.charge_account = False
            form.fields.pop('charge_account', None)
            form.fields.pop('account_limit', None)
        addresses = CustomerAddressFormSet(request.POST if request.method == 'POST' and 'addresses-TOTAL_FORMS' in request.POST else None, instance=record, prefix='addresses')
        valid = form.is_valid() if request.method == 'POST' else False
        addresses_valid = addresses.is_valid() if addresses.is_bound else True
        if request.method == 'POST' and valid and addresses_valid:
            if pk and form.cleaned_data['version'] != version:
                form.add_error(None, 'Someone changed this customer. Reload before saving.')
            else:
                record = form.save(commit=False)
                if not pk:
                    record.source_id = 'local:' + str(uuid.uuid4())
                if pk:
                    save_local_edit(record, version, [*CustomerForm.Meta.fields, 'full_name'])
                else:
                    record.version = 1
                    record.locally_edited_at = timezone.now()
                    record.save()
                if addresses.is_bound:
                    record.addresses.update(is_default=False)
                    addresses.instance = record
                    addresses.save()
                    for address_form in addresses.forms:
                        values=address_form.cleaned_data
                        if values and not values.get('DELETE') and values.get('is_default') and address_form.instance.pk:
                            record.addresses.filter(pk=address_form.instance.pk).update(is_default=True)
                destination = dict(parse_qsl(urlsplit(return_to).query))
                destination.update(customer=record.pk, saved='1' if pk else 'added')
                return redirect('/customers/?' + urlencode(destination) + '#customer-details')
    return render(request, 'customers/edit.html', {
        'title': 'Edit Customer' if pk else 'Add Customer', 'form': form,
        'record': record, 'addresses': addresses, 'copied': copied, 'return_to': return_to,
    }, status=400 if request.method == 'POST' else 200)


@never_cache
@require_http_methods(['POST'])
@register_required('manage_charge_accounts')
def charge_account(request, pk):
    with transaction.atomic():
        customer = get_object_or_404(Customer.objects.select_for_update(), pk=pk)
        # Enable only this flag; preserve limits, balances and source evidence.
        Customer.objects.filter(pk=pk).update(charge_account=True)
        return JsonResponse({'ok': True, 'charge_account': True,
                             'account_balance': str(customer.account_balance),
                             'account_limit': str(customer.account_limit)})


@require_safe
@never_cache
def customer_list(request):
    query = request.GET.get('q', '').strip()[:200]
    status = request.GET.get('status', 'active')
    sort = request.GET.get('sort', 'full_name')
    if sort.lstrip('-') not in {'full_name', 'first_name', 'last_name', 'company', 'city', 'customer_number', 'phone', 'street', 'email', 'mobile', 'state', 'postal_code', 'imported_last_sale', 'imported_account_balance'}:
        sort = 'full_name'
    customers = Customer.objects.all()
    if query:
        # Every search word must match, but intervening text and word order do not matter.
        for term in query.split():
            match = Q()
            for field in ('full_name', 'company', 'phone', 'mobile', 'email', 'city', 'customer_number', 'street', 'state', 'postal_code', 'alternate_phone', 'alternate_phone_2', 'alternate_contact', 'notes', 'customer_type'):
                match |= Q(**{field + '__icontains': term})
            customers = customers.filter(match)
    if status in {'active', 'inactive'}:
        customers = customers.filter(active=status == 'active')
    else:
        status = 'all'
    # Account Balance column/sort = QB balance at the backup + charges and payments on account in this app.
    customers = customers.annotate(balance_total=F('imported_account_balance') + F('app_account_activity'))
    customers = customers.order_by(sort.replace('imported_account_balance', 'balance_total'), 'source_id')
    if request.GET.get('format') == 'rows':
        raw_offset = request.GET.get('offset', '0')
        if not raw_offset.isascii() or not raw_offset.isdigit() or len(raw_offset) > 9:
            return JsonResponse({'error': 'Invalid customer offset.'}, status=400)
        offset = int(raw_offset)
        total = customers.count()
        fields = ('id', 'full_name', 'first_name', 'company', 'customer_number', 'source_id',
                  'phone', 'street', 'city', 'email', 'mobile', 'state', 'postal_code', 'track_as_company',
                  'imported_last_sale', 'balance_total')
        rows = list(customers.values(*fields)[offset:offset + 100])
        for row in rows:
            row['display_name'] = ((row['company'] if row.pop('track_as_company') else '') or row.pop('full_name') or row['company'] or
                                   row['customer_number'] or row['source_id'])
            row.pop('source_id')
            last = row.pop('imported_last_sale')
            row['last_sale'] = f'{last:%m/%d/%Y}' if last else ''
            balance = row.pop('balance_total')
            row['balance'] = f'${balance:,.2f}' if balance else ''
        return JsonResponse({'rows': rows, 'offset': offset, 'total': total})
    page = Paginator(customers, 50).get_page(request.GET.get('page'))
    selected = None
    if request.GET.get('customer', '').isdigit():
        selected = get_object_or_404(Customer, pk=request.GET['customer'])
    params = {'q': query, 'status': status, 'sort': sort}
    if request.GET.get('view') == 'pages':
        params['view'] = 'pages'
    # Load just the display-name index so scrollbar jumps show a name immediately,
    # even before the corresponding batch of full customer rows arrives.
    names = []
    if params.get('view') != 'pages':
        rows = customers.values_list('track_as_company', 'full_name', 'company',
                                     'customer_number', 'source_id').iterator(chunk_size=2000)
        names = [(company if as_company else '') or full_name or company or number or source_id
                 for as_company, full_name, company, number, source_id in rows]
    return render(request, 'customers.html', {
        'page': page, 'query': query, 'status': status, 'sort': sort,
        'selected': selected, 'params': urlencode(params), 'sort_fields':['full_name','phone','street','city','email','mobile','company','state','postal_code','customer_number','imported_last_sale','imported_account_balance'],
        'scroll_config': {**params, 'total': page.paginator.count,
                          'names': names,
                          'start': page.start_index() - 1 if page.object_list else 0,
                          'selected': selected.pk if selected else None},
        'total_customers': Customer.objects.count(), 'saved': {'1': 'Customer saved.', 'added': 'Customer added.'}.get(request.GET.get('saved', '')),
        **(dict(stats=detail.statistics(selected), history=detail.history(selected), address_options=detail.addresses(selected),
                open_charges=detail.open_charges(selected))
           if selected else {}),
    })
