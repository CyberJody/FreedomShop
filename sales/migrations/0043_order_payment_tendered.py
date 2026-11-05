# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):
    """the cash handed over on a Cash order payment (Amount received), shown with the change on the order and its
    printout. Additive and nullable; existing payments stay blank (no backfill). Not money: payments, tax locks and
    Daily Totals still count OrderPayment.amount."""

    dependencies = [
        ('sales', '0042_order_payment_cashier'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderpayment',
            name='tendered',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
