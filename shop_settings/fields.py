# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department.tax_category: a foreign key to TaxCategory that also takes a category name, as the old text field did
(the QuickBooks importer and existing code write "Taxable Sales" / "Non")."""
from django.db import models
from django.db.models.fields.related_descriptors import ForwardManyToOneDescriptor


class _CategoryDescriptor(ForwardManyToOneDescriptor):
    def __set__(self, instance, value):
        if isinstance(value, str):
            from .config import category_named
            value = category_named(value)
        super().__set__(instance, value)


class TaxCategoryField(models.ForeignKey):
    forward_related_accessor_class = _CategoryDescriptor
