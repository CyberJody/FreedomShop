# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations


def forward(apps,schema_editor):
    Vendor=apps.get_model('purchasing','Vendor')
    for vendor in Vendor.objects.all().iterator():
        source=vendor.source_data if isinstance(vendor.source_data,dict) else {}
        values=source.get('values',{})
        if not isinstance(values,dict): continue
        vendor.contact=' '.join(values[k] for k in ('FName','LName') if isinstance(values.get(k),str))[:200]
        vendor.account_number=values.get('AccNum','') if isinstance(values.get('AccNum'),str) else ''
        vendor.save(update_fields=['contact','account_number'])
    Department=apps.get_model('purchasing','Department')
    for dep in Department.objects.all().iterator():
        if dep.tax_category.casefold().replace(' ','').replace('-','') in {'non','nontaxable','nontaxablesales','notax','taxexempt'}:
            dep.tax_rate=0;dep.save(update_fields=['tax_rate'])


class Migration(migrations.Migration):
    dependencies=[('purchasing','0008_department_tax_rate_vendor_account_number_and_more')]
    operations=[migrations.RunPython(forward,migrations.RunPython.noop)]
