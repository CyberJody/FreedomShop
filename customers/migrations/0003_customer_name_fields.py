# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('customers', '0002_customer_locally_edited_at_customer_version')]

    operations = [
        migrations.AddField(model_name='customer', name='first_name', field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='customer', name='last_name', field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='customer', name='title', field=models.CharField(blank=True, max_length=50)),
    ]
