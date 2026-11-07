# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.permissions import register_can
from decimal import Decimal, ROUND_HALF_UP
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.http import require_safe
from django.views.decorators.cache import never_cache
from sales.models import Item, plain, quantity_text, rate_text
from .item_views import search_items
from .catalog_quantities import catalog_quantities

@never_cache
@require_safe
def item_picker(request):
    query=request.GET.get('q','').strip()[:100];vendor=request.GET.get('vendor','')
    if vendor and (not vendor.isascii() or not vendor.isdigit() or len(vendor)>18):return JsonResponse({'error':'Invalid vendor.'},status=400)
    qs=catalog_quantities(search_items(Item.objects.filter(active=True),query)).prefetch_related('alternate_vendors').order_by('name','pk')
    page=Paginator(qs,40).get_page(request.GET.get('page'));rows=[]
    from sales.holds import available_by_item  # Available = on hand - open orders - held
    free=available_by_item(list(page))
    for item in page:
        cost=item.order_cost
        if vendor and str(item.primary_vendor_id)!=vendor:
            alternate=next((v for v in item.alternate_vendors.all() if str(v.vendor_id)==vendor),None)
            cost=alternate.order_cost if alternate else None
        rows.append(dict(id=item.pk,sku=item.sku,label=item.display_name,description=item.description,on_hand=quantity_text(item.on_hand) if item.on_hand is not None else None,available=quantity_text(free[item.pk]) if free[item.pk] is not None else None,price=plain(item.price),tax_rate=rate_text(item.tax_rate) if item.tax_rate is not None else None,vendor=item.vendor,cost=str(cost.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if cost is not None else '',on_order=quantity_text(item.on_order),last_received=item.last_received.strftime('%m/%d/%Y') if item.last_received else ''))
    if not register_can(request, 'see_costs'):
        for row in rows: row.pop('cost', None)
    return JsonResponse(dict(results=rows,count=page.paginator.count,page=page.number,pages=page.paginator.num_pages))
