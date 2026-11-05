# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB, wording): a blank average cost reads "Not received yet" when the on-hand count is known
(a new item before its first receiving) and stays "Unverified" when the count itself is unknown (imported, NULL)."""
import json
from decimal import Decimal as D

from django.test import RequestFactory, TestCase

from sales.models import Item
from .catalog_quantities import catalog_quantities
from .item_scrolling import item_rows

AVG_COST = 11  # Item List column index of Avg Cost


class AverageCostLabelTests(TestCase):
    def setUp(self):
        self.new = Item.objects.create(sku='R71NEW', name='R71-NEW', description='New chair', price=D('100'),
                                       on_hand=D('0'), average_cost=None)
        self.imported = Item.objects.create(sku='R71OLD', name='R71-OLD', description='Imported chair', price=D('100'),
                                            on_hand=None, average_cost=None)
        self.costed = Item.objects.create(sku='R71COST', name='R71-COST', description='Received chair', price=D('100'),
                                          on_hand=D('3'), average_cost=D('40'))

    def row(self, item):
        response = item_rows(RequestFactory().get('/', {'offset': '0'}), catalog_quantities(Item.objects.filter(pk=item.pk)), 'inventory')
        return json.loads(response.content)['rows'][0]['cells']

    def test_label_property(self):
        self.assertEqual(self.new.average_cost_missing_label, 'Not received yet')
        self.assertEqual(self.imported.average_cost_missing_label, 'Unverified')

    def test_item_list_scroll_rows(self):
        self.assertEqual((self.row(self.new)[2], self.row(self.new)[AVG_COST]), ('0', 'Not received yet'))
        self.assertEqual((self.row(self.imported)[2], self.row(self.imported)[AVG_COST]), ('Unverified', 'Unverified'))
        self.assertEqual(self.row(self.costed)[AVG_COST], '$40.00')

    def test_item_list_first_page(self):
        listing = self.client.get('/purchasing/items/', {'q': 'R71'}).content.decode()
        self.assertIn('<td>Not received yet</td>', listing)
        self.assertIn('<td>Unverified</td><td>Unverified</td>', listing)  # imported: Order Cost and Avg Cost both unknown

    def test_item_details_panel(self):
        new = self.client.get(f'/purchasing/items/{self.new.pk}/details/').content.decode()
        self.assertIn('<dt>Avg. Unit Cost</dt><dd>Not received yet</dd>', new)
        self.assertIn('<span class="detail-qty">0</span>', new)
        old = self.client.get(f'/purchasing/items/{self.imported.pk}/details/').content.decode()
        self.assertIn('<dt>Avg. Unit Cost</dt><dd>Unverified</dd>', old)
        self.assertIn('<dt>On Hand Qty</dt><dd><span class="detail-qty">Unverified</span>', old)
        self.assertNotIn('Not received yet', old)
        costed = self.client.get(f'/purchasing/items/{self.costed.pk}/details/').content.decode()
        self.assertIn('<dt>Avg. Unit Cost</dt><dd>$40.00</dd>', costed)
