# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax locations and the settings row from what an existing database already holds: one location per distinct
non-blank tax location name on documents and customers (an "Exempt"-named one becomes the exempt location), plus an
exempt location if there was none. Taxable locations get the most common taxable department rate; the most used
taxable location becomes the default (blank names have always meant it). A new, empty database is left alone: the
post-migrate step creates the seed configuration instead (shop_settings.config.ensure_configured)."""
from collections import Counter

from django.db import migrations
from shop_settings.normalization import location_key


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Document = apps.get_model('sales', 'Document')
    OrderPayment = apps.get_model('sales', 'OrderPayment')
    Customer = apps.get_model('customers', 'Customer')
    Department = apps.get_model('purchasing', 'Department')
    TaxLocation = apps.get_model('shop_settings', 'TaxLocation')
    TaxSettings = apps.get_model('shop_settings', 'TaxSettings')
    if not (Document.objects.using(db).exists() or Customer.objects.using(db).exists() or Department.objects.using(db).exists()):
        return
    used = Counter()
    names = {}
    for model, field in ((Document, 'tax_location'), (Customer, 'tax_location'),
                         (Document, 'locked_location'), (OrderPayment, 'locked_location')):
        for name in model.objects.using(db).values_list(field, flat=True):
            key = location_key(name)
            if key:
                used[key] += 1
                display = name.strip()
                names[key] = min(names.get(key, display), display)
    rates = Counter(Department.objects.using(db).filter(tax_rate__gt=0).values_list('tax_rate', flat=True))
    rate = rates.most_common(1)[0][0] if rates else 0
    for n, key in enumerate(sorted(used)):
        name = names[key]
        exempt = key == location_key('Exempt')
        TaxLocation.objects.using(db).get_or_create(name=name[:100], defaults=dict(rate=0 if exempt else rate, is_exempt=exempt, sort=n))
    if not TaxLocation.objects.using(db).filter(is_exempt=True).exists():
        TaxLocation.objects.using(db).create(name='Exempt', rate=0, is_exempt=True, sort=99)
    taxable = [key for key, _ in used.most_common() if key != location_key('Exempt')]
    default = (TaxLocation.objects.using(db).get(name=names[taxable[0]][:100]) if taxable
               else TaxLocation.objects.using(db).create(name='Local', rate=rate))
    if not TaxSettings.objects.using(db).exists():
        # An existing shop must choose its filing year; never silently assume a calendar year.
        TaxSettings.objects.using(db).create(default_location=default, default_rate=rate, tax_year_start_month=None)


class Migration(migrations.Migration):
    dependencies = [
        ('shop_settings', '0001_initial'),
        ('sales', '0047_order_hold_for_pickup'),
        ('customers', '0009_customer_number_index'),
        ('purchasing', '0018_truck_receiving'),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
