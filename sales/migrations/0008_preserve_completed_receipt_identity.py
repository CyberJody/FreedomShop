# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations


def forward(apps,schema_editor):
    Document=apps.get_model('sales','Document')
    Document.objects.filter(kind='sale',completed_at__isnull=False).update(was_completed=True)
    Document.objects.filter(kind='sale',status='completed').update(was_completed=True)


class Migration(migrations.Migration):
    dependencies=[('sales','0007_document_customer_po_document_payment_breakdown_and_more')]
    operations=[migrations.RunPython(forward,migrations.RunPython.noop)]
