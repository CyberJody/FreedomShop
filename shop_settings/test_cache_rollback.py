# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transaction-local settings snapshots must never survive rollback."""
from decimal import Decimal as D

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from shop_settings.config import business, clear_cache, tax
from shop_settings.models import Business, TaxSettings


class RollbackChecks:
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)

    def test_tax_and_business_reads_cannot_escape_a_rolled_back_savepoint(self):
        rate, name = tax().default_rate, business().name
        for helper, model, field, value, expected in ((tax, TaxSettings, 'default_rate', D('9'), rate),
                                                     (business, Business, 'name', 'Rolled Back Shop', name)):
            with self.subTest(helper=helper.__name__):
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        row = model.objects.get()
                        setattr(row, field, value)
                        row.save()
                        self.assertEqual(getattr(helper(), field), value)
                        raise RuntimeError('Rollback this settings edit')
                self.assertEqual(getattr(model.objects.get(), field), expected)
                self.assertEqual(getattr(helper(), field), expected)


class RollbackCacheTests(RollbackChecks, TestCase):
    pass


class OuterRollbackCacheTests(RollbackChecks, TransactionTestCase):
    """The same checks when the rolled-back block is the outermost transaction."""
