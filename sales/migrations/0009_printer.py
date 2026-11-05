# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fork migration: the original private-store data step is intentionally omitted.

Existing databases remain the store owner's responsibility; this migration does not
remove or repair their data. New installations configure their own users and printers.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('sales', '0008_preserve_completed_receipt_identity')]
    operations = [
        migrations.CreateModel(name='Printer', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('name', models.CharField(max_length=100, unique=True)),
            ('kind', models.CharField(choices=[('receipt', 'Receipt (Star, 72mm)'), ('letter', 'Letter (sales orders)')], default='receipt', max_length=10, verbose_name='Type')),
            ('model', models.CharField(blank=True, max_length=100)),
            ('location', models.CharField(blank=True, max_length=100)),
            ('host', models.CharField(max_length=100, verbose_name='IP address or hostname')),
            ('port', models.PositiveIntegerField(default=9100)),
            ('mac_address', models.CharField(blank=True, max_length=17, verbose_name='MAC address')),
            ('serial_number', models.CharField(blank=True, max_length=100)),
            ('notes', models.TextField(blank=True)),
            ('active', models.BooleanField(default=True)),
            ('version', models.PositiveIntegerField(default=1)),
            ('updated_at', models.DateTimeField(auto_now=True)),
        ], options={'ordering': ['name'], 'constraints': [models.CheckConstraint(condition=models.Q(port__gte=1, port__lte=65535), name='printer_port_range')]}),
    ]
