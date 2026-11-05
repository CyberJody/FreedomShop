# SPDX-License-Identifier: AGPL-3.0-or-later
from urllib.parse import parse_qs, urlsplit
from django.test import TestCase
from sales.models import Item
from .item_scrolling import item_return_url


class ItemScrollingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Item.objects.bulk_create([Item(sku=f'S{i:04}', name=f'Inventory {i:04}',
            description='Verified description', price='10', active=i != 202) for i in range(203)])

    def test_batches_cover_the_list_without_gaps_and_match_name_index(self):
        count = 202
        page = self.client.get('/purchasing/items/')
        config = page.context['item_scroll_config']
        names, ids = [], []
        for offset in [0, 100, 200, 300]:
            response = self.client.get('/purchasing/items/', {'format':'rows', 'offset':offset})
            self.assertIn('no-store', response['Cache-Control'])
            data = response.json()
            self.assertEqual(data['total'], count)
            self.assertEqual(len(data['rows']), min(100, max(0, count-offset)))
            names.extend(r['name'] for r in data['rows'])
            ids.extend(r['id'] for r in data['rows'])
            for row in data['rows']:
                self.assertEqual(len(row['cells']), 17)  # + Held
                self.assertEqual(row['href'], f"/purchasing/items/{row['id']}/")
        self.assertEqual(names, config['names'])
        self.assertEqual(len(set(ids)), count)

    def test_search_status_and_fallback_pages(self):
        result = self.client.get('/purchasing/items/', {'q':'0101', 'format':'rows'}).json()
        self.assertEqual(result['total'], 1)
        self.assertTrue(result['rows'][0]['name'].endswith('0101'))
        page = self.client.get('/purchasing/items/', {'view':'pages','page':3})
        self.assertEqual(page.context['page'].start_index(),101)
        self.assertEqual(page.context['item_scroll_config']['names'],[])
        inactive = self.client.get('/purchasing/items/', {'status':'inactive','format':'rows'}).json()
        self.assertEqual([r['name'] for r in inactive['rows']], ['Inventory 0202'])
        empty = self.client.get('/purchasing/items/', {'q':'no matches','format':'rows'}).json()
        self.assertEqual(empty['rows'], [])

    def test_invalid_offsets_are_rejected(self):
        for offset in ['-1','a','1.5','999999999999','１２']:
            self.assertEqual(self.client.get('/purchasing/items/', {'format':'rows','offset':offset}).status_code,400)

    def test_blank_name_does_not_turn_into_a_number_and_markup_is_data(self):
        item = Item.objects.first()
        item.name=''; item.save()
        data = self.client.get('/purchasing/items/', {'format':'rows'}).json()
        self.assertEqual(data['rows'][0]['name'], 'Unnamed item')
        item.name='</script><script>alert(1)</script>'; item.save()
        page = self.client.get('/purchasing/items/')
        self.assertNotContains(page, item.name)
        self.assertIn(item.name,page.context['item_scroll_config']['names'])

    def test_return_context_and_external_urls(self):
        target = '/purchasing/items/?q=Inventory&at=130&page=3'
        item = Item.objects.first()
        page = self.client.get(f'/purchasing/items/{item.pk}/', {'return_to':target})
        self.assertEqual(page.context['return_to'], target)
        for bad in ['https://example.com/purchasing/items/', '//example.com/purchasing/items/', '/pos/']:
            self.assertEqual(item_return_url(bad), '')
        self.assertNotIn('evil', item_return_url(target+'&evil=1'))
