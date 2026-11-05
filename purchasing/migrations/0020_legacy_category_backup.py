# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup column also exists on databases which applied the first Build 1 conversion.

Original strings already discarded by that conversion cannot be recovered; NULL distinguishes those rows and
new departments from strings (including blanks) backed up by the corrected conversion.
"""
from django.db import migrations


def add_if_missing(apps, schema_editor):
    Department = apps.get_model('purchasing', 'Department')
    with schema_editor.connection.cursor() as cursor:
        columns = schema_editor.connection.introspection.get_table_description(cursor, Department._meta.db_table)
    if 'legacy_tax_category' not in {column.name for column in columns}:
        schema_editor.add_field(Department, Department._meta.get_field('legacy_tax_category'))


class Migration(migrations.Migration):
    dependencies = [('purchasing', '0019_department_tax_category_fk')]
    # The field is already in the corrected 0019 state. Keep reversal there responsible for removing it.
    operations = [migrations.RunPython(add_if_missing, migrations.RunPython.noop)]
