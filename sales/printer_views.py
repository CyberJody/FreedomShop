# SPDX-License-Identifier: AGPL-3.0-or-later
"""File > Printers: list, add/edit, live status and test prints for store printers."""
import re

from django import forms
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .models import Document, Printer
from .printing import PrintError, check_status, print_test_page


class PrinterForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['order_copies'].required = False

    class Meta:
        model = Printer
        fields = ['name', 'kind', 'model', 'location', 'host', 'port', 'mac_address', 'serial_number', 'color', 'media_source', 'order_copies', 'notes', 'active']
        widgets = {'notes': forms.Textarea(attrs={'rows': 3})}

    def clean_order_copies(self):
        # blank (a receipt or label printer, or a form opened before this field existed) keeps what it had.
        value = self.cleaned_data.get('order_copies')
        if value is not None:
            return value
        from shop_settings.config import business
        return self.instance.order_copies or business().order_copies_default

    def clean_host(self):
        host = self.cleaned_data['host'].strip()
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,100}', host):
            raise forms.ValidationError('Enter an IP address such as 192.0.2.10 or a hostname.')
        return host

    def clean_mac_address(self):
        mac = self.cleaned_data['mac_address'].strip().upper().replace('-', ':')
        if mac and not re.fullmatch(r'([0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
            raise forms.ValidationError('Enter a MAC address such as 00:00:5E:00:53:2E, or leave it blank.')
        return mac


@require_safe
@never_cache
def printer_list(request):
    status = request.GET.get('status', 'active')
    printers = Printer.objects.all()
    if status == 'active':
        printers = printers.filter(active=True)
    elif status == 'inactive':
        printers = printers.filter(active=False)
    else:
        status = 'all'
    return render(request, 'printers/list.html', dict(title='Printers', printers=printers, status=status, message=request.GET.get('saved') == '1'))


@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def printer_edit(request, pk=None):
    with transaction.atomic():
        printer = get_object_or_404(Printer.objects.select_for_update(), pk=pk) if pk else Printer()
        version = printer.version
        initial = {'version': version}
        if not pk:
            from shop_settings.config import business
            initial['order_copies'] = business().order_copies_default
        form = PrinterForm(request.POST if request.method == 'POST' else None, instance=printer, initial=initial)
        if request.method == 'POST' and form.is_valid():
            if pk and form.cleaned_data.get('version') != version:
                form.add_error(None, 'Someone changed this printer. Reload before saving.')
            else:
                try:
                    with transaction.atomic():
                        record = form.save(commit=False)
                        record.version = version + 1 if pk else 1
                        record.save()
                    return redirect('/printers/?saved=1')
                except IntegrityError:
                    form.add_error('name', 'Another printer already has this name.')
    return render(request, 'printers/edit.html', dict(title=('Edit Printer' if pk else 'Add Printer'), form=form, printer=printer),
                  status=400 if request.method == 'POST' else 200)


@require_safe
@never_cache
def printer_status(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    if not printer.active:
        # Inactive printers may be busy with live QB POS work: never open a connection to them.
        return JsonResponse({'ok': False, 'status': 'Inactive (not checked)'})
    return JsonResponse(check_status(printer))


@never_cache
@require_http_methods(['POST'])
def printer_test(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    try:
        print_test_page(printer)
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    return JsonResponse({'message': f'Test page sent to {printer.name}.'})


@never_cache
@require_http_methods(['POST'])
def print_sales_order(request, token):
    from .letter_printing import render_sales_order, send_letter
    order = get_object_or_404(Document.objects.select_related('customer'), token=token, kind='order')
    key = request.POST.get('printer', '')
    printer = Printer.objects.filter(pk=key, active=True, kind='letter').first() if key.isdigit() and len(key) <= 18 else None
    if not printer:
        return JsonResponse({'error': 'Choose a sales order printer.'}, status=400)
    copies = request.POST.get('copies', '1')
    if not copies.isdigit() or not 1 <= int(copies) <= 5:
        return JsonResponse({'error': 'Choose 1 to 5 copies.'}, status=400)
    try:
        send_letter(printer, render_sales_order(order), f'Sales Order {order.number}', int(copies))
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    copies_label = '1 copy' if copies == '1' else f'{copies} copies'
    return JsonResponse({'message': f'Sent {order.numbered_label} to {printer.name} ({copies_label}).'})


def _document(source, token):
    from django.http import Http404
    from .print_documents import load
    try:
        return load(source, token), None
    except Http404:
        return None, JsonResponse({'error': 'This document is not available for printing.'}, status=404)
    except PrintError as exc:
        return None, JsonResponse({'error': str(exc)}, status=400)


@never_cache
@require_http_methods(['POST'])
def print_document(request, source, token):
    from .print_documents import PDF, print_to, remember, resolve
    loaded, error = _document(source, token)
    if error:
        return error
    doc, kind, title = loaded
    try:
        target = resolve(kind, request.POST.get('target', ''))
        if target == PDF:
            return JsonResponse({'pdf_url': f'/print/{source}/{token}/pdf/'})
        remember(request, kind, str(target.pk))
        name = print_to(target, doc, kind, title)
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    return JsonResponse({'message': f'Sent {title} to {name}.'})


@never_cache
@require_safe
def pdf_document(request, source, token):
    from django.http import Http404, HttpResponse
    from .print_documents import pdf
    loaded, error = _document(source, token)
    if error:
        raise Http404
    doc, kind, title = loaded
    response = HttpResponse(pdf(doc, kind, title), content_type='application/pdf')
    safe = re.sub(r'[^A-Za-z0-9-]+', '-', title).strip('-')
    response['Content-Disposition'] = f'inline; filename="{safe}.pdf"'
    return response


@never_cache
@require_http_methods(['POST'])
def print_choice(request):
    from .print_documents import PDF, remember, resolve
    kind = request.POST.get('kind')
    if kind not in {'receipt', 'order'}:
        return JsonResponse({'error': 'Unknown document type.'}, status=400)
    try:
        target = resolve(kind, request.POST.get('target', ''))
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    remember(request, kind, PDF if target == PDF else str(target.pk))
    return JsonResponse({'ok': True})
