# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


def one_copy(apps, schema_editor):
    """every existing furniture set tag was saved at the old default of 2 copies; set them to 1."""
    apps.get_model('tags', 'FurnitureSet').objects.update(copies=1)


class Migration(migrations.Migration):

    dependencies = [
        ('tags', '0002_furnituresetline_sku'),
    ]

    operations = [
        migrations.AlterField(
            model_name='furnitureset',
            name='copies',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RunPython(one_copy, migrations.RunPython.noop),
    ]
