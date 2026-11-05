# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid

from django.db import migrations, models


def identify_businesses(apps, schema_editor):
    Business = apps.get_model('shop_settings', 'Business')
    for row in Business.objects.using(schema_editor.connection.alias).all():
        row.exchange_id = uuid.uuid4()
        row.save(update_fields=['exchange_id'])


class Migration(migrations.Migration):
    dependencies = [('shop_settings', '0006_business_setup')]
    operations = [
        migrations.AddField(model_name='business', name='exchange_id', field=models.UUIDField(null=True, editable=False)),
        migrations.RunPython(identify_businesses, migrations.RunPython.noop),
        migrations.AlterField(model_name='business', name='exchange_id',
                              field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
    ]
