# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations


def restore(apps, schema_editor):
    Customer=apps.get_model('customers','Customer')
    text={'customer_type':'2','alternate_phone':'25','alternate_phone_2':'26','notes':'28','tax_location':'36'}
    flags={'track_as_company':'6','email_opt_in':'52','accept_checks':'55','do_not_mail':'57'}
    pending=[]
    for customer in Customer.objects.all().iterator(chunk_size=500):
        source=customer.source_data if isinstance(customer.source_data,dict) else {}
        cols=source.get('columns',{}) if source.get('table')=='Customer' else {}
        if not isinstance(cols,dict): cols={}
        for field,col in text.items():
            value=cols.get(col)
            if isinstance(value,str): setattr(customer,field,value)
        for field,col in flags.items():
            value=cols.get(col)
            if type(value) in (int,bool) and value in (0,1): setattr(customer,field,bool(value))
        for field,key in [('alternate_phone','phone3'),('alternate_phone_2','phone4')]:
            if not getattr(customer,field) and isinstance(source.get(key),str): setattr(customer,field,source[key])
        pending.append(customer)
        if len(pending)>=500:
            Customer.objects.bulk_update(pending,[*text,*flags],batch_size=500);pending=[]
    if pending: Customer.objects.bulk_update(pending,[*text,*flags],batch_size=500)


class Migration(migrations.Migration):
    dependencies=[('customers','0005_customer_tax_location')]
    operations=[migrations.RunPython(restore,migrations.RunPython.noop)]
