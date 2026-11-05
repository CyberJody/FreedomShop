# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):
    """who took an order payment or refund. Additive; existing payments stay blank (no backfill)."""

    dependencies = [
        ('sales', '0041_order_payment_card_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderpayment',
            name='cashier',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
