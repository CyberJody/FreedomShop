# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department.tax_category: free text -> a TaxCategory row. One category per distinct non-blank name already on
departments ("Non"-style names are non-taxable, the rest taxable), each department linked to its own; a blank name
gets the first taxable category, or the first non-taxable one for a 0 % department (as the old rule read blanks);
generic names ("Taxable Sales", "Non") sort first, then the most used, so they are the defaults.
On an empty database nothing is created here; the post-migrate step seeds the categories. New departments'
default rate is the shop default rate (Tax Settings) instead of a fixed 7."""
from collections import Counter

from django.db import migrations, models

import shop_settings.config
import shop_settings.fields

NON_KEYS = {'non', 'nontaxable', 'nontaxablesales', 'notax', 'taxexempt'}
GENERIC_KEYS = NON_KEYS | {'tax', 'taxable', 'taxablesales'}


def key(name):
    return (name or '').strip().casefold().replace('-', '').replace(' ', '')


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Department = apps.get_model('purchasing', 'Department')
    TaxCategory = apps.get_model('shop_settings', 'TaxCategory')
    departments = list(Department.objects.using(db).all())
    if not departments:
        return
    used = Counter(d.tax_category for d in departments if (d.tax_category or '').strip())
    # Generic names ("Taxable Sales", "Non") first, then the most used: the first of each kind is the default one.
    names = sorted(used, key=lambda name: (key(name) not in GENERIC_KEYS, -used[name], name.casefold(), name))
    by_name = {}
    for n, name in enumerate(names):
        by_name[name] = TaxCategory.objects.using(db).get_or_create(
            name=name, defaults=dict(taxable=key(name) not in NON_KEYS, sort=n))[0]

    def first(taxable, fallback):
        row = TaxCategory.objects.using(db).filter(taxable=taxable).order_by('sort', 'name').first()
        return row or TaxCategory.objects.using(db).create(name=fallback, taxable=taxable, sort=99)

    for d in departments:
        name = d.tax_category
        d.legacy_tax_category = name
        if (name or '').strip():
            d.tax_category_ref_id = by_name[name].pk
        else:
            d.tax_category_ref_id = (first(False, 'Non-taxable') if d.tax_rate == 0 else first(True, 'Taxable')).pk
        d.save(update_fields=['tax_category_ref', 'legacy_tax_category'])


def backwards(apps, schema_editor):
    Department = apps.get_model('purchasing', 'Department')
    for d in Department.objects.using(schema_editor.connection.alias).select_related('tax_category_ref'):
        d.tax_category = (d.legacy_tax_category if d.legacy_tax_category is not None
                          else d.tax_category_ref.name if d.tax_category_ref_id else '')
        d.save(update_fields=['tax_category'])


class Migration(migrations.Migration):
    dependencies = [
        ('purchasing', '0018_truck_receiving'),
        ('shop_settings', '0002_locations_from_documents'),
    ]
    operations = [
        migrations.AddField('department', 'legacy_tax_category', models.CharField(max_length=100, null=True, editable=False)),
        migrations.AddField('department', 'tax_category_ref', models.ForeignKey(
            null=True, blank=True, on_delete=models.PROTECT, related_name='+', to='shop_settings.taxcategory')),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField('department', 'tax_category'),
        migrations.RenameField('department', 'tax_category_ref', 'tax_category'),
        migrations.AlterField('department', 'tax_category', shop_settings.fields.TaxCategoryField(
            null=True, blank=True, default=shop_settings.config.default_tax_category, on_delete=models.PROTECT,
            related_name='departments', to='shop_settings.taxcategory')),
        migrations.AlterField('department', 'tax_rate', models.DecimalField(
            decimal_places=3, default=shop_settings.config.default_tax_rate, max_digits=6)),
    ]
