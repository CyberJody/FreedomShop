# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings screens (admin only): Tax Settings at /settings/tax/, Business Settings at /settings/business/."""
import mimetypes

from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from accounts.admin_mode import admin_mode_required
from .config import business, tax
from .forms import BusinessForm, CategoryForm, GeneralForm, LocationForm
from .models import Business, TaxCategory, TaxLocation, TaxSettings
from .normalization import references_name

SAVED = {'general': 'Tax settings saved.', 'location': 'Tax location saved.', 'location-added': 'Tax location added.',
         'location-retired': 'Tax location retired: it stays on older sales but is no longer offered.',
         'location-restored': 'Tax location restored.', 'location-deleted': 'Tax location deleted.',
         'category': 'Tax category saved.', 'category-added': 'Tax category added.',
         'category-retired': 'Tax category retired: departments that have it keep it.', 'category-restored': 'Tax category restored.',
         'category-deleted': 'Tax category deleted.'}


def location_in_use(location):
    from customers.models import Customer
    from sales.models import Document, OrderPayment
    return (references_name(Document.objects.all(), 'tax_location', location.name)
            or references_name(Document.objects.all(), 'locked_location', location.name)
            or references_name(Document.objects.all(), 'account_estimate_location', location.name)
            or references_name(OrderPayment.objects.all(), 'locked_location', location.name)
            or references_name(Customer.objects.all(), 'tax_location', location.name)
            or TaxSettings.objects.filter(default_location=location).exists())


def category_in_use(category):
    from sales.models import Item
    return category.departments.exists() or references_name(Item.objects.all(), 'tax_category', category.name)


def _location_refusal(location, action):
    """Why a location can't be retired or deleted (None when it can)."""
    config = tax()
    if location.name == config.default_location.name:
        return 'This is the default tax location. Choose another default first.'
    if location.is_exempt and location.active and not TaxLocation.objects.filter(is_exempt=True, active=True).exclude(pk=location.pk).exists():
        return 'Tax-exempt sales need an exempt location; this is the only one.'
    if action == 'delete' and location_in_use(location):
        return 'This location is on sales or customers, so it can only be retired.'
    return None


def _category_refusal(category, action):
    if category.active and not TaxCategory.objects.filter(taxable=category.taxable, active=True).exclude(pk=category.pk).exists():
        return f'This is the only {"taxable" if category.taxable else "non-taxable"} category.'
    if action == 'delete' and category_in_use(category):
        return 'Departments or items use this category, so it can only be retired.'
    return None


@admin_mode_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def tax_settings_view(request):
    tax()   # creates the rows from the seed if this database has none yet
    settings_row = TaxSettings.objects.select_related('default_location').get()
    general = GeneralForm(instance=settings_row, initial={'version': settings_row.version})
    new_location, new_category, edit_forms, error, status = LocationForm(prefix='new-loc'), CategoryForm(prefix='new-cat'), {}, '', 200
    if request.method == 'POST':
        action = request.POST.get('action', '')
        with transaction.atomic():
            if action == 'general':
                settings_row = TaxSettings.objects.select_for_update().get()
                general = GeneralForm(request.POST, instance=settings_row)
                if general.is_valid():
                    if general.cleaned_data.get('version') != settings_row.version:
                        general.add_error(None, 'Someone changed the tax settings. Reload before saving.')
                    else:
                        row = general.save(commit=False)
                        row.version += 1
                        row.save()
                        return redirect('/settings/tax/?saved=general')
            elif action == 'add-location':
                new_location = LocationForm(request.POST, prefix='new-loc')
                if new_location.is_valid():
                    new_location.save()
                    return redirect('/settings/tax/?saved=location-added#locations')
            elif action == 'add-category':
                new_category = CategoryForm(request.POST, prefix='new-cat')
                if new_category.is_valid():
                    new_category.save()
                    return redirect('/settings/tax/?saved=category-added#categories')
            elif action in ('location', 'location-retire', 'location-restore', 'location-delete'):
                location = get_object_or_404(TaxLocation.objects.select_for_update(), pk=request.POST.get('pk', '0') if request.POST.get('pk', '').isdigit() else 0)
                if action == 'location':
                    form = LocationForm(request.POST, instance=location, prefix=f'loc-{location.pk}')
                    if form.is_valid():
                        form.save()
                        return redirect('/settings/tax/?saved=location#locations')
                    edit_forms[('loc', location.pk)] = form
                elif action == 'location-restore':
                    location.active = True
                    location.save(update_fields=['active'])
                    return redirect('/settings/tax/?saved=location-restored#locations')
                else:
                    error = _location_refusal(location, 'delete' if action == 'location-delete' else 'retire')
                    if not error:
                        if action == 'location-delete':
                            location.delete()
                            return redirect('/settings/tax/?saved=location-deleted#locations')
                        location.active = False
                        location.save(update_fields=['active'])
                        return redirect('/settings/tax/?saved=location-retired#locations')
            elif action in ('category', 'category-retire', 'category-restore', 'category-delete'):
                category = get_object_or_404(TaxCategory.objects.select_for_update(), pk=request.POST.get('pk', '0') if request.POST.get('pk', '').isdigit() else 0)
                if action == 'category':
                    original_taxable = category.taxable
                    role_refusal = _category_refusal(category, 'retire')
                    form = CategoryForm(request.POST, instance=category, prefix=f'cat-{category.pk}', in_use=category_in_use(category))
                    if form.is_valid():
                        if form.cleaned_data['taxable'] != original_taxable:
                            error = role_refusal
                        if error:
                            form.add_error('taxable', error)
                        else:
                            form.save()
                            return redirect('/settings/tax/?saved=category#categories')
                    edit_forms[('cat', category.pk)] = form
                elif action == 'category-restore':
                    category.active = True
                    category.save(update_fields=['active'])
                    return redirect('/settings/tax/?saved=category-restored#categories')
                else:
                    error = _category_refusal(category, 'delete' if action == 'category-delete' else 'retire')
                    if not error:
                        if action == 'category-delete':
                            category.delete()
                            return redirect('/settings/tax/?saved=category-deleted#categories')
                        category.active = False
                        category.save(update_fields=['active'])
                        return redirect('/settings/tax/?saved=category-retired#categories')
            else:
                error = 'Unknown action.'
        status = 400
    locations = []
    for location in TaxLocation.objects.all():
        form = edit_forms.get(('loc', location.pk)) or LocationForm(instance=location, prefix=f'loc-{location.pk}')
        locations.append(dict(row=location, form=form, in_use=location_in_use(location), default=location.pk == settings_row.default_location_id))
    categories = []
    for category in TaxCategory.objects.all():
        in_use = category_in_use(category)
        form = edit_forms.get(('cat', category.pk)) or CategoryForm(instance=category, prefix=f'cat-{category.pk}', in_use=in_use)
        categories.append(dict(row=category, form=form, in_use=in_use, departments=category.departments.count()))
    return render(request, 'settings/tax.html', dict(
        title='Tax Settings', general=general, settings_row=settings_row, locations=locations, categories=categories,
        new_location=new_location, new_category=new_category, error=error, saved=SAVED.get(request.GET.get('saved', ''), '')), status=status)


@admin_mode_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def business_settings_view(request):
    business()   # creates the placeholder row on first use
    row = Business.objects.get()
    form = BusinessForm(instance=row, initial={'version': row.version})
    if request.method == 'POST':
        with transaction.atomic():
            row = Business.objects.select_for_update().get()
            old_logo = row.logo.name
            form = BusinessForm(request.POST, request.FILES, instance=row)
            if form.is_valid():
                if form.cleaned_data.get('version') != row.version:
                    form.add_error(None, 'Someone changed the business settings. Reload before saving.')
                else:
                    saved = form.save(commit=False)
                    if form.cleaned_data.get('remove_logo') and not request.FILES.get('logo'):
                        saved.logo = ''
                    saved.version += 1
                    saved.save()
                    if old_logo and old_logo != saved.logo.name:
                        transaction.on_commit(lambda: default_storage.delete(old_logo))
                    return redirect('/settings/business/?saved=1')
        row.refresh_from_db()
    return render(request, 'settings/business.html', dict(title='Business Settings', form=form, row=row, saved=request.GET.get('saved') == '1'),
                  status=400 if request.method == 'POST' else 200)


@admin_mode_required
@require_safe
def business_logo(request):
    """The uploaded logo, for the preview on the settings screen."""
    row = business()
    if not row.logo or not default_storage.exists(row.logo.name):
        raise Http404
    return FileResponse(default_storage.open(row.logo.name, 'rb'), content_type=mimetypes.guess_type(row.logo.name)[0] or 'application/octet-stream')


@admin_mode_required
@require_safe
def test_receipt(request):
    """"Print test receipt": the receipt header and footer as a PNG, for the screen only (never sent to a printer)."""
    from sales.label_printing import png_bytes
    from sales.printing import render_sample_receipt
    response = HttpResponse(png_bytes(render_sample_receipt()), content_type='image/png')
    response['Cache-Control'] = 'no-store'
    return response
