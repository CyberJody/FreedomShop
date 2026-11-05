# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use a generic register group, preserving existing memberships without creating users."""
from django.db import migrations


def rename_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    groups = Group.objects.using(schema_editor.connection.alias)
    previous = groups.filter(name='Register').first()
    if previous is None:
        return
    current = groups.filter(name='register').first()
    if current is None:
        previous.name = 'register'
        previous.save(using=schema_editor.connection.alias, update_fields=['name'])
    else:
        current.user_set.add(*previous.user_set.all())
        current.permissions.add(*previous.permissions.all())
        previous.delete(using=schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0003_register_group')]
    operations = [migrations.RunPython(rename_group, migrations.RunPython.noop)]
