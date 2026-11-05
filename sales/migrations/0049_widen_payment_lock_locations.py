# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('sales', '0048_account_estimate_snapshot')]
    operations = [
        migrations.AlterField(model_name=model, name='locked_location',
                              field=models.CharField(max_length=100, blank=True, editable=False))
        for model in ('document', 'orderpayment')
    ]
