# SPDX-License-Identifier: AGPL-3.0-or-later
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .models import GiftCertificate, DiscountReason
from django.views.decorators.http import require_http_methods
from django.shortcuts import redirect
from django.db import IntegrityError, transaction


@require_http_methods(['GET', 'POST'])
def discount_reasons(request):
    error = ''
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        pk = request.POST.get('id')
        if not name or len(name) > 100:
            error = 'Enter a discount reason of 1 to 100 characters.'
        else:
            try:
                with transaction.atomic():
                    if pk:
                        row = DiscountReason.objects.get(pk=pk)
                        row.name = name
                        row.active = request.POST.get('active') == 'on'
                        row.save()
                    else:
                        DiscountReason.objects.create(name=name)
                return redirect('discount-reasons')
            except (IntegrityError, DiscountReason.DoesNotExist, ValueError):
                error = 'That reason already exists, or the list changed. Check the entry.'
    return render(request, 'sales/discount-reasons.html', {'title': 'Discount Reasons', 'reasons': DiscountReason.objects.all(), 'error': error})


@require_GET
def balances(request):
    q = request.GET.get('q', '').strip()[:100]
    certificates = GiftCertificate.objects.select_related('issued_on')
    if q:
        certificates = certificates.filter(number__icontains=q)
    return render(request, 'sales/gift-balances.html', {'title': 'Gift Certificate Balances', 'certificates': certificates, 'q': q})


@require_GET
def lookup(request):
    cert = GiftCertificate.objects.filter(number=request.GET.get('number', '')[:100]).first()
    if cert is None:
        return JsonResponse({'error': 'Unknown gift certificate. No verified balance is recorded.'}, status=404)
    return JsonResponse({'number': cert.number, 'balance': str(cert.balance)})
