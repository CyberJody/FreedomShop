# SPDX-License-Identifier: AGPL-3.0-or-later
"""the Item List says "Item saved" / "Item added" after its edit pop-up saves and closes, and selects the edited item."""
from django.test import TestCase

from sales.models import Item
from .models import Vendor


class ItemSavedNoteTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='R69', company='R69 Vendor')
        self.data = dict(name='R69-CHAIR', item_type='inventory', description='Test chair', price='100', on_hand='',
                         average_cost='', order_cost='', tax_category='Taxable Sales', tax_rate='7',
                         primary_vendor=self.vendor.pk, active='on', version='1')

    def test_popup_save_of_a_new_item_tells_the_list_it_was_added(self):
        response = self.client.post('/purchasing/items/new/?modal=1', {**self.data})
        item = Item.objects.get(name='R69-CHAIR')
        self.assertEqual(response.status_code, 302)
        self.assertIn('added=1', response.url)
        page = self.client.get(response.url).content.decode()
        self.assertIn(f"send('item-modal-saved',{{id:{item.pk},added:true}})", page)

    def test_popup_save_of_an_existing_item_tells_the_list_it_was_saved(self):
        self.client.post('/purchasing/items/new/', {**self.data})
        item = Item.objects.get(name='R69-CHAIR')
        response = self.client.post(f'/purchasing/items/{item.pk}/?modal=1', {**self.data, 'size': '42x72', 'version': str(item.version)})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('added=1', response.url)
        page = self.client.get(response.url).content.decode()
        self.assertIn(f"send('item-modal-saved',{{id:{item.pk},added:false}})", page)

    def test_save_and_new_still_starts_a_blank_item(self):
        response = self.client.post('/purchasing/items/new/?modal=1', {**self.data, 'action': 'save-next'})
        self.assertNotIn('added=1', response.url)
        self.assertIn("send('item-modal-next')", self.client.get(response.url).content.decode())

    def test_item_list_shows_the_note_and_selects_the_saved_item(self):
        page = self.client.get('/purchasing/items/').content.decode()
        self.assertIn("text:e.data.added?'Item added':'Item saved'", page)
        self.assertIn("box.className='item-saved-note'", page)
        self.assertIn("note.text==='Item saved'){selected.clear();selected.add(note.id);}", page)
