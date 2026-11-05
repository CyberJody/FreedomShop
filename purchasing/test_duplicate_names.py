# SPDX-License-Identifier: AGPL-3.0-or-later
"""warn before saving an item whose name another active item already uses."""
from django.test import TestCase

from sales.models import Item


class DuplicateItemNameTests(TestCase):
    def setUp(self):
        self.existing = Item.objects.create(sku='6045', name='RRF-R15-MAH', description='Rocker', price='100', tax_rate='7')
        self.data = dict(name='  rrf-r15-mah ', item_type='inventory', description='Another rocker', price='120', on_hand='0',
                         tax_category='Taxable Sales', tax_rate='7', active='on', version='1')

    def test_new_item_with_a_used_name_is_refused_until_confirmed(self):
        response = self.client.post('/purchasing/items/new/', self.data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Item.objects.count(), 1)  # a stale page can't skip the question
        self.assertContains(response, 'Item #6045 already uses this name. Save anyway?', status_code=400)
        self.assertContains(response, 'id="same-name-dialog"', status_code=400)
        self.assertContains(response, 'Save anyway', status_code=400)
        self.assertContains(response, 'ask(document.getElementById', status_code=400)  # the dialog opens on the re-shown form
        response = self.client.post('/purchasing/items/new/', {**self.data, 'confirm_duplicate_name': '1'})
        self.assertEqual(response.status_code, 302)  # a warning, not a block
        self.assertEqual(Item.objects.filter(name__iexact='rrf-r15-mah').count(), 2)

    def test_edit_warns_only_when_the_name_changes_to_a_used_one(self):
        other = Item.objects.create(sku='7001', name='OTHER-NAME', price='5', tax_rate='7', version=1)
        url = f'/purchasing/items/{other.pk}/'
        self.assertEqual(self.client.post(url, {**self.data, 'name': 'RRF-R15-MAH'}).status_code, 400)
        other.refresh_from_db()
        self.assertEqual(other.name, 'OTHER-NAME')
        self.assertEqual(self.client.post(url, {**self.data, 'name': 'RRF-R15-MAH', 'confirm_duplicate_name': '1'}).status_code, 302)
        other.refresh_from_db()
        self.assertEqual(other.name, 'RRF-R15-MAH')
        # Saving it again (price change, same name) doesn't ask again.
        self.assertEqual(self.client.post(url, {**self.data, 'name': 'RRF-R15-MAH', 'price': '6', 'version': '2'}).status_code, 302)

    def test_inactive_items_and_the_item_itself_do_not_count(self):
        self.existing.active = False
        self.existing.save()
        self.assertEqual(self.client.post('/purchasing/items/new/', self.data).status_code, 302)
        mine = Item.objects.latest('pk')
        self.assertEqual(self.client.get('/purchasing/items/name-check/', {'name': 'RRF-R15-MAH', 'item': mine.pk}).json()['skus'], [])

    def test_name_check_endpoint_uses_item_numbers(self):
        Item.objects.create(sku='7002', name='rrf-r15-mah', price='5', tax_rate='7')
        answer = self.client.get('/purchasing/items/name-check/', {'name': ' RRF-R15-MAH '}).json()
        self.assertEqual(answer, {'skus': ['6045', '7002'], 'message': 'Items #6045, #7002 already use this name. Save anyway?'})
        self.assertEqual(self.client.get('/purchasing/items/name-check/', {'name': 'unused'}).json(), {'skus': [], 'message': ''})
        self.assertEqual(self.client.get('/purchasing/items/name-check/', {'name': ''}).json()['skus'], [])
        self.assertEqual(self.client.post('/purchasing/items/name-check/', {'name': 'x'}).status_code, 405)
