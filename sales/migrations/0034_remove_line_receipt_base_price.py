# SPDX-License-Identifier: AGPL-3.0-or-later
"""`receipt_base_price` is replaced by `receipt_discount` (0033 moved the data). Separate migration so the
schema change runs in its own transaction after the data fix (Postgres refuses ALTER TABLE after row updates with
pending deferred triggers)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0033_qb_unit_prices_and_receipt_discounts'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='line',
            name='receipt_base_price',
        ),
    ]
