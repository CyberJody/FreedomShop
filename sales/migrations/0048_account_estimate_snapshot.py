# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freeze the previous estimate terms once for existing history; future documents carry their own snapshot.

There is no historical rate timeline in the old schema. The upgrade retains the estimate last available to the
old report. It never guesses a later rate for a historical payment after this snapshot has been taken.
"""
from django.db import migrations, models
import shop_settings.config


def freeze_existing(apps, schema_editor):
    db = schema_editor.connection.alias
    settings = apps.get_model('shop_settings', 'TaxSettings').objects.using(db).select_related('default_location').first()
    location = settings.default_location if settings else None
    apps.get_model('sales', 'Document').objects.using(db).update(
        account_estimate_rate=location.rate if location else 0,
        account_estimate_location=location.name if location else 'Local')


class Migration(migrations.Migration):
    dependencies = [('sales', '0047_order_hold_for_pickup'), ('shop_settings', '0002_locations_from_documents')]
    operations = [
        migrations.AddField('document', 'account_estimate_rate', models.DecimalField(max_digits=6, decimal_places=3, null=True, editable=False)),
        migrations.AddField('document', 'account_estimate_location', models.CharField(max_length=100, default='', editable=False)),
        migrations.RunPython(freeze_existing, migrations.RunPython.noop),
        migrations.AlterField('document', 'account_estimate_rate', models.DecimalField(max_digits=6, decimal_places=3, default=shop_settings.config.default_estimate_rate, editable=False)),
        migrations.AlterField('document', 'account_estimate_location', models.CharField(max_length=100, default=shop_settings.config.default_estimate_location, editable=False)),
    ]
