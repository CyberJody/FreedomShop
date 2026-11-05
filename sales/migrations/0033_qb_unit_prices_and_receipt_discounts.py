# SPDX-License-Identifier: AGPL-3.0-or-later
"""unit prices as QuickBooks keeps them and receipt discounts allocated per line.

- Line.price widens to 4 decimals (a % line discount leaves 1,379.95 x 90 % = 1241.9550; typed prices stay whole cents).
- Line.discount_percent: the % off the regular price on that line (QB DiscountPercent).
- Line.receipt_discount: the line's share of the receipt-level % discount (QB RealPrice). Prices and extensions stay
  gross; Subtotal is gross; Total = Subtotal - Discount + Tax, as on QB's receipt.
- Data fix for app-made receipts saved under the old scheme (discounted unit price in `price`, the regular price in
  `receipt_base_price`, net subtotal): price goes back to the regular price, the line's discount becomes
  receipt_discount = qty x (regular - discounted) (what the old code summed into Document.discount, so the receipt's
  discount, tax and total do not move), and the subtotal becomes gross (subtotal + discount). Imported QB rows are
  untouched; `manage.py backfill_unit_prices` refreshes their 4-decimal prices and per-line shares from the dataset.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations, models


def cents(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def forward(apps, schema_editor):
    Line = apps.get_model('sales', 'Line')
    Document = apps.get_model('sales', 'Document')
    alias = schema_editor.connection.alias
    lines = Line.objects.using(alias).filter(source_id__isnull=True, receipt_base_price__isnull=False, document__source_id__isnull=True)
    for line in lines.iterator(chunk_size=1000):
        line.receipt_discount = cents(line.quantity * (line.receipt_base_price - line.price))
        line.price = line.receipt_base_price
        line.receipt_base_price = None
        line.save(update_fields=['receipt_discount', 'price', 'receipt_base_price'])
    docs = Document.objects.using(alias).filter(kind='sale', source_id__isnull=True).exclude(discount=0)
    for doc in docs.iterator(chunk_size=1000):
        doc.subtotal = doc.subtotal + doc.discount
        doc.save(update_fields=['subtotal'])


def backward(apps, schema_editor):
    Line = apps.get_model('sales', 'Line')
    Document = apps.get_model('sales', 'Document')
    alias = schema_editor.connection.alias
    for line in Line.objects.using(alias).filter(source_id__isnull=True, document__source_id__isnull=True).exclude(receipt_discount=0).iterator(chunk_size=1000):
        line.receipt_base_price = line.price
        line.price = cents(line.price - line.receipt_discount / line.quantity) if line.quantity else line.price
        line.save(update_fields=['receipt_base_price', 'price'])
    for doc in Document.objects.using(alias).filter(kind='sale', source_id__isnull=True).exclude(discount=0).iterator(chunk_size=1000):
        doc.subtotal = doc.subtotal - doc.discount
        doc.save(update_fields=['subtotal'])


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0032_seed_discount_reasons'),
    ]

    operations = [
        migrations.AlterField(
            model_name='line',
            name='price',
            field=models.DecimalField(decimal_places=4, max_digits=16),
        ),
        migrations.AddField(
            model_name='line',
            name='discount_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name='line',
            name='receipt_discount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=16),
        ),
        migrations.RunPython(forward, backward),
    ]
