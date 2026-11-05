# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-only upload → review mappings → import, and guarded undo."""
from django import forms
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.admin_mode import admin_mode_required
from shop_settings.config import tax
from shop_settings.models import TaxSettings

from .models import ImportLog
from .qbpos_files import MAX_BYTES, ImportError, parse_file
from .qbpos_import import file_metadata, import_exports, mapping_proposals, normalized_options, undo_import


class UploadForm(forms.Form):
    items = forms.FileField(required=False, label='Items export')
    customers = forms.FileField(required=False, label='Customers export')
    vendors = forms.FileField(required=False, label='Vendors export')
    sales = forms.FileField(required=False, label='Sales History report (optional)')
    opening_voucher = forms.BooleanField(required=False, initial=True, label='Create opening receiving vouchers dated today, one per department')

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(kind) for kind in ('items', 'customers', 'vendors', 'sales')):
            raise forms.ValidationError('Choose at least one export file.')
        return cleaned


def preview_summary(payload, options):
    try:
        return import_exports(payload, options), ''
    except ImportError as exc:
        return {'warnings': [warning for file in payload.values() for warning in file['warnings']]}, str(exc)


@admin_mode_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            payload = {kind: parse_file(kind, file.name, file.read(MAX_BYTES + 1))
                       for kind in ('items', 'customers', 'vendors', 'sales') if (file := form.cleaned_data.get(kind))}
            options = normalized_options(payload, {'opening_voucher': form.cleaned_data['opening_voucher']})
            summary, error = preview_summary(payload, options)
            summary['error'] = error
            log = ImportLog.objects.create(created_by=request.user, files=file_metadata(payload),
                                           payload=payload, options=options, summary=summary)
            return redirect('qbpos-preview', import_id=log.pk)
        except ImportError as exc:
            form.add_error(None, str(exc))
    return render(request, 'settings/qbpos-upload.html', {'title': 'Migrate from QuickBooks POS', 'form': form})


@admin_mode_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def preview(request, import_id):
    error = ''
    with transaction.atomic():
        tax()
        TaxSettings.objects.select_for_update().get()
        log = get_object_or_404(ImportLog.objects.select_for_update(), pk=import_id)
        if log.status != 'preview':
            return redirect('qbpos-imports')
        if request.method == 'POST':
            if request.POST.get('action') == 'review':
                proposals = mapping_proposals(log.payload, log.options)
                options = {'opening_voucher': request.POST.get('opening_voucher') == 'on',
                           'tax_map': {row['code']: request.POST.get(f'tax_{i}', '') for i, row in enumerate(proposals['tax_codes'])},
                           'department_map': {row['name']: request.POST.get(f'department_{i}', '') for i, row in enumerate(proposals['departments'])}}
                # Parse IDs before ORM filters, so tampered inputs are form errors.
                if any(value and not value.isdigit() for field in ('tax_map', 'department_map') for value in options[field].values()):
                    error = 'Select a listed category or department.'
                else:
                    log.options = options
                    log.summary, error = preview_summary(log.payload, options)
                    log.summary['error'] = error
                    log.save(update_fields=['options', 'summary'])
            elif request.POST.get('action') == 'import' and not log.summary.get('error'):
                try:
                    import_exports({}, commit=True, log=log, user=request.user)
                    return redirect('qbpos-imports')
                except ImportError as exc:
                    error = str(exc)
    proposals = mapping_proposals(log.payload, log.options)
    return render(request, 'settings/qbpos-preview.html', {'title': 'Review migration', 'log': log,
                  'summary': log.summary, 'error': error or log.summary.get('error', ''), **proposals})


@admin_mode_required
@never_cache
@require_safe
def imports(request):
    from django.core.paginator import Paginator
    page = Paginator(ImportLog.objects.defer('payload', 'changes').all(), 50).get_page(request.GET.get('page'))
    return render(request, 'settings/qbpos-imports.html', {'title': 'Migration imports', 'page': page, 'error': request.GET.get('error', '')})


@admin_mode_required
@never_cache
@require_POST
def undo(request, import_id):
    try:
        undo_import(import_id)
    except ImportError as exc:
        from urllib.parse import urlencode
        return redirect('/settings/migrate-quickbooks/imports/?' + urlencode({'error': str(exc)}))
    return redirect('qbpos-imports')
