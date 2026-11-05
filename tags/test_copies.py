# SPDX-License-Identifier: AGPL-3.0-or-later
"""furniture set tags default to 1 copy (existing ones set to 1 by migration); the sales order Copies box
always starts at 3 (the printer choice is still remembered)."""
import importlib
import re
from pathlib import Path

from django.apps import apps as django_apps
from django.test import TestCase, override_settings

from sales.models import Item
from .models import DEFAULT_TAGLINE, FurnitureSet

SCRIPT = Path(__file__).resolve().parents[1] / 'templates/sales/editor-script.html'


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class OneCopyTagTests(TestCase):
    def setUp(self):
        self.chair = Item.objects.create(sku='7601', name='TEST R76 CHAIR', price='100.00', tax_rate='7.000', description='TEST R76 CHAIR')

    def test_new_set_defaults_to_one_copy(self):
        self.assertEqual(FurnitureSet().copies, 1)
        page = self.client.get('/tags/furniture-sets/new/').content.decode()
        self.assertIn('id="copies-field" value="1"', page)
        # A post with no copies (or a bad value) saves 1.
        self.client.post('/tags/furniture-sets/new/', dict(name='TEST R76 set', tagline=DEFAULT_TAGLINE, item=[self.chair.pk], quantity=['1'], then='stay'))
        tag_set = FurnitureSet.objects.get(name='TEST R76 set')
        self.assertEqual(tag_set.copies, 1)
        self.client.post(f'/tags/furniture-sets/{tag_set.token}/', dict(name='TEST R76 set', tagline=DEFAULT_TAGLINE, copies='9', item=[self.chair.pk], quantity=['1'], then='stay'))
        tag_set.refresh_from_db()
        self.assertEqual(tag_set.copies, 1)
        page = self.client.get(f'/tags/furniture-sets/{tag_set.token}/').content.decode()
        self.assertIn('<option selected>1</option>', page)

    def test_migration_sets_existing_tags_to_one(self):
        FurnitureSet.objects.create(name='TEST old default', copies=2)
        FurnitureSet.objects.create(name='TEST three', copies=3)
        module = importlib.import_module('tags.migrations.0003_furnitureset_one_copy')
        module.one_copy(django_apps, None)
        self.assertEqual(set(FurnitureSet.objects.values_list('copies', flat=True)), {1})


class OrderCopiesStartAtThree(TestCase):
    def test_copies_box_is_not_remembered_but_the_printer_is(self):
        source = SCRIPT.read_text()
        self.assertNotIn("localStorage.setItem('orderCopies'", source)
        self.assertNotIn("remember(printCopies", source)
        self.assertIn("localStorage.removeItem('orderCopies')", source)
        self.assertRegex(source, r"localStorage\.setItem\(receiptPrinter\.dataset\.remember")
        bar = (Path(__file__).resolve().parents[1] / 'templates/sales/printer-bar.html').read_text()
        # the box starts at the printer's own sales order copies (3 unless a printer says otherwise), never at a
        # number remembered from an earlier page.
        self.assertIn('{% with first_copies=doc_printers.0.order_copies', bar)
        self.assertTrue(re.search(r'<select id="print-copies" autocomplete="off">.*n == first_copies %\} selected', bar))
        self.assertIn('data-copies="{{ printer.order_copies }}"', bar)
