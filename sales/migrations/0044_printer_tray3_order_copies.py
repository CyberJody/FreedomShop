# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fork migration: the original private-store data step is intentionally omitted.

Existing databases remain the store owner's responsibility; this migration does not
remove or repair their data. New installations configure their own users and printers.
"""
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0043_order_payment_tendered'),
    ]

    operations = [
        migrations.AddField(
            model_name='printer',
            name='order_copies',
            field=models.PositiveSmallIntegerField(default=3, help_text='Letter printers: copies a sales order starts at on this printer (1 to 5).', validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(5)], verbose_name='Sales order copies'),
        ),
        migrations.AlterField(
            model_name='printer',
            name='media_source',
            field=models.CharField(blank=True, choices=[('', 'Auto (printer chooses)'), ('tray-1', 'Tray 1'), ('tray-2', 'Tray 2'), ('tray-3', 'Tray 3'), ('tray-4', 'Tray 4'), ('manual', 'Manual feed (MP tray)')], help_text='Letter printers: which tray to print from.', max_length=20, verbose_name='Paper tray'),
        ),
    ]
