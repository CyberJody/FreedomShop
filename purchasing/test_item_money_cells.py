# SPDX-License-Identifier: AGPL-3.0-or-later
"""the Item List's rows drawn by the page itself (what shows right after the edit pop-up saves and the list
reloads, and in Use pages view) format money exactly like the rows the scrolling list loads: $ and two places."""
import re
from decimal import Decimal

from django.test import TestCase

from sales.models import Item


class ItemListMoneyCellsTests(TestCase):
    def setUp(self):
        Item.objects.create(sku='R83-1', name='R83 A TABLE', description='Table', price=Decimal('499.95'),
                            order_cost=Decimal('211.00'), average_cost=Decimal('205.5'), tax_rate=7)
        Item.objects.create(sku='R83-2', name='R83 B SOFA', description='Sofa', price=Decimal('1234.50'),
                            order_cost=None, average_cost=None, tax_rate=7)
        Item.objects.create(sku='R83-3', name='R83 C LAMP', description='Lamp', price=None, order_cost=Decimal('0'),
                            average_cost=Decimal('0'), tax_rate=7)

    def page_cells(self, query=''):
        html = self.client.get('/purchasing/items/' + query).content.decode()
        body = html[html.index('<tbody>'):html.index('</tbody>')]
        rows = re.findall(r'<tr data-catalog-id="(\d+)">(.*?)</tr>', body, re.S)
        return {int(pk): [re.sub(r'<[^>]+>', '', c) for c in re.findall(r'<td[^>]*>(.*?)</td>', cells, re.S)] for pk, cells in rows}

    def test_page_rows_match_the_scrolling_rows(self):
        loaded = {r['id']: r['cells'] for r in self.client.get('/purchasing/items/?format=rows&offset=0').json()['rows']}
        for query in ('', '?view=pages'):
            drawn = self.page_cells(query)
            self.assertEqual(set(drawn), set(loaded))
            for pk, cells in drawn.items():
                for column in (4, 10, 11):  # Regular Price, Order Cost, Avg Cost
                    with self.subTest(query=query, item=pk, column=column):
                        self.assertEqual(cells[column], loaded[pk][column])
        table = Item.objects.get(sku='R83-1').pk
        self.assertEqual([self.page_cells()[table][i] for i in (4, 10, 11)], ['$499.95', '$211.00', '$205.50'])
        sofa = Item.objects.get(sku='R83-2').pk
        self.assertEqual(self.page_cells()[sofa][4], '$1234.50')
        self.assertEqual(self.page_cells()[sofa][10], 'Unverified')
        self.assertEqual(self.page_cells()[Item.objects.get(sku='R83-3').pk][4], 'Enter price')
