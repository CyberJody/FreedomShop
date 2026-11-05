# SPDX-License-Identifier: AGPL-3.0-or-later
"""Also widen the schema for databases that already applied the first Build 1 migration."""
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shop_settings', '0003_business')]
    operations = [
        migrations.AlterField('taxsettings', 'tax_year_start_month', models.PositiveSmallIntegerField(
            choices=[(1, 'January'), (2, 'February'), (3, 'March'), (4, 'April'), (5, 'May'), (6, 'June'),
                     (7, 'July'), (8, 'August'), (9, 'September'), (10, 'October'), (11, 'November'), (12, 'December')],
            default=1, null=True, validators=[MinValueValidator(1), MaxValueValidator(12)])),
    ]
