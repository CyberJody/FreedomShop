# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tag Generator: printable furniture set tags, replacing a held-receipt workaround in QuickBooks POS."""
import uuid

from django.db import models

DEFAULT_TAGLINE = 'THIS SET AVAILABLE NOW\nRESERVE YOURS TODAY!'


class FurnitureSet(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=200, help_text='For finding the set later; not printed.')
    tagline = models.TextField(max_length=300, default=DEFAULT_TAGLINE, blank=True)
    copies = models.PositiveSmallIntegerField(default=1)  # tags print 1 copy unless changed
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'pk']

    def __str__(self):
        return self.name


class FurnitureSetLine(models.Model):
    tag_set = models.ForeignKey(FurnitureSet, on_delete=models.CASCADE, related_name='lines')
    position = models.PositiveSmallIntegerField(default=0)
    # Prices and descriptions are read from the Item List at print time; the snapshot covers a deleted item.
    item = models.ForeignKey('sales.Item', on_delete=models.SET_NULL, null=True, related_name='+')
    sku = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=200, blank=True)
    description = models.CharField(max_length=1000, blank=True)
    quantity = models.DecimalField(max_digits=9, decimal_places=3, default=1)

    def save(self, *args, **kwargs):
        if self.item_id:
            self.sku = self.item.sku
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'sku'}
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['position', 'pk']
