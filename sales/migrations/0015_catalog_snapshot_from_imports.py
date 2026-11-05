# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal, InvalidOperation
from django.db import migrations


def populate(apps, schema_editor):
    Item=apps.get_model('sales','Item');Source=apps.get_model('sales','ImportedRecord');History=apps.get_model('sales','HistoricalRecord');Snapshot=apps.get_model('sales','ItemCatalogSnapshot')
    db=schema_editor.connection.alias
    items={i.sku:i.pk for i in Item.objects.using(db).all()}
    latest={}
    for receipt in History.objects.using(db).filter(kind='voucher',record_type='Receiving',status='Regular').only('pk','occurred_at','lines').order_by('-occurred_at','-pk').iterator(chunk_size=300):
        for line in receipt.lines:
            sku=str(line.get('item_number') or '')
            try:positive=Decimal(str(line.get('quantity') or 0))>0
            except InvalidOperation:positive=False
            if sku in items and positive:latest.setdefault(items[sku],receipt.pk)
    pending=[]
    for source in Source.objects.using(db).filter(kind='item',target_model='sales.Item').iterator(chunk_size=500):
        if source.target_id not in items.values():continue
        value=source.data.get('On Order Qty')
        try:quantity=Decimal(str(value)) if value is not None else None
        except InvalidOperation:quantity=None
        pending.append(Snapshot(item_id=source.target_id,source_id=source.pk,on_order=quantity,last_receipt_id=latest.get(source.target_id)))
    Snapshot.objects.using(db).bulk_create(pending,batch_size=500)


class Migration(migrations.Migration):
    dependencies=[('sales','0014_itemnumbercounter_itemcatalogsnapshot')]
    operations=[migrations.RunPython(populate,migrations.RunPython.noop)]
