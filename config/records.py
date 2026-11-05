# SPDX-License-Identifier: AGPL-3.0-or-later
"""Delete (deactivate) a customer, vendor or department from its list without breaking saved history; items go to the Items Trash."""
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from customers.models import Customer
from sales.models import Item
from purchasing.models import Vendor, Department

@never_cache
@require_http_methods(['GET','POST'])
@transaction.atomic
def remove_record(request, section, pk):
    models={'customers':Customer,'items':Item,'vendors':Vendor,'departments':Department}
    model=models.get(section)
    if not model: raise Http404
    if section=='items': return redirect(f'/purchasing/items/delete/?item={pk}')  # Items Trash
    record=get_object_or_404(model.objects.select_for_update(),pk=pk)
    destination='/customers/' if section=='customers' else f'/purchasing/{section}/'
    error=''
    if request.method=='POST':
        if str(record.version)!=request.POST.get('version'): error='This record changed. Reload before deleting it.'
        else:
            record.active=False;record.version+=1
            fields=['active','version']
            if section=='customers': record.locally_edited_at=timezone.now();fields.append('locally_edited_at')
            record.save(update_fields=fields)
            return redirect(destination)
    label=getattr(record,'display_name',None) or str(record)
    return render(request,'delete-record.html',dict(title='Delete Record',record=record,label=label,destination=destination,error=error),status=400 if error else 200)
