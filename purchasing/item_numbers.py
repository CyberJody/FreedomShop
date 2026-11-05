# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allocate internal item numbers inside the caller's save transaction."""
from sales.models import Item, ItemNumberCounter

def next_item_number():
    counter,_=ItemNumberCounter.objects.get_or_create(pk=1)
    counter=ItemNumberCounter.objects.select_for_update().get(pk=counter.pk)
    if not counter.last_number:
        counter.last_number=max((int(s) for s in Item.objects.values_list('sku',flat=True) if s.isascii() and s.isdigit() and len(s)<=18),default=0)
    candidate=counter.last_number+1
    while Item.objects.filter(sku=str(candidate)).exists():candidate+=1
    counter.last_number=candidate;counter.save(update_fields=['last_number'])
    return str(candidate)
