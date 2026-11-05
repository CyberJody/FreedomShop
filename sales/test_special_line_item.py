# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): changing a special-order line on a sales order updates its item (and its open PO lines)."""
import uuid
from decimal import Decimal as D

from django.test import TestCase

from customers.models import Customer
from purchasing.models import Department, PurchaseDocument, Vendor
from purchasing.services import save_purchase
from purchasing.test_po_cancel import header
from sales.models import Document, Item
from sales.services import save_document
from sales.views import document_data

OLD_NAME = 'SR/210-OAK-SEELY/COFFEE SOLD'
OLD_DESC = '(SOLD DUNHAM) COMFURT SIDE CHAIR. RED OAK, SEAT IN OCS-104 SEELY, ALL ELSE OCS-226 COFFEE'
NEW_NAME = 'SR/210-OAK-CAPP SOLD'
NEW_DESC = '(SOLD DUNHAM) COMFURT SIDE CHAIR. RED OAK FINISHED ALL OCS-119 CAPPUCCINO'


class SpecialLineFollowsItemTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='SR', company='SAMPLE RUSTIC')
        self.spe = Department.objects.create(code='SPE', name='SPECIAL ORDER', tax_rate=D('7'))
        self.customer = Customer.objects.create(source_id='r96-1', full_name='PAT EXAMPLE', customer_number='R96-1')
        self.chair = Item.objects.create(sku='17055', name=OLD_NAME, description=OLD_DESC, price=D('329.95'), on_hand=0,
                                         average_cost=0, order_cost=D('140'), primary_vendor=self.vendor, department=self.spe, tax_rate=7)
        self.stock = Item.objects.create(sku='2171', name='CIC-R-025A-PINECONE', description='13 X 36 PINE CONE TABLE RUNNER',
                                         price=D('34.95'), on_hand=9, average_cost=15, order_cost=15, primary_vendor=self.vendor, tax_rate=7)
        self.order = self.save_new([self.chair])

    def save_new(self, items, customer=None):
        token = str(uuid.uuid4())
        lines = [dict(item_id=i.pk, item_name=i.display_name, description=i.description, quantity='6', price=str(i.price), tax_rate='7')
                 for i in items]
        return save_document(dict(token=token, version=0, customer_id=(customer or self.customer).pk, lines=lines, action='save',
                                  notes=''), 'order', token)

    def edit(self, order, name, description, item=None):
        data = document_data(order)
        for row in data['lines']:
            if item is None or row['item_id'] == item.pk:
                row['item_name'], row['description'] = name, description
        data['action'] = 'save'
        return save_document(data, 'order', order.token)

    def po_for(self, order, item):
        line = order.lines.get(item=item)
        return save_purchase(header(self.vendor), [dict(item=item, quantity=D('6'), cost=D('140'), sales_line=line.pk)], 'po', 'save')

    def test_special_item_follows_the_line(self):
        saved = self.edit(self.order, NEW_NAME, NEW_DESC)
        self.chair.refresh_from_db()
        self.assertEqual((self.chair.name, self.chair.description), (NEW_NAME, NEW_DESC))
        self.assertEqual(saved.notes_for_screen, ['Item #17055 in the Item List was changed to match this order.'])
        line = saved.lines.get()
        self.assertEqual((line.item_name, line.description), (NEW_NAME, NEW_DESC))

    def test_unchanged_save_leaves_item_alone(self):
        self.chair.description = 'EDITED IN THE ITEM LIST'; self.chair.save()
        saved = self.edit(self.order, OLD_NAME, OLD_DESC)   # nothing typed on the order this time
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.description, 'EDITED IN THE ITEM LIST')
        self.assertEqual(saved.notes_for_screen, [])

    def test_stock_item_edit_stays_on_the_line(self):
        order = self.save_new([self.stock])
        saved = self.edit(order, 'RUNNER FOR MRS SMITH', 'GIFT WRAP PLEASE')
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.name, self.stock.description), ('CIC-R-025A-PINECONE', '13 X 36 PINE CONE TABLE RUNNER'))
        self.assertEqual(saved.lines.get().description, 'GIFT WRAP PLEASE')

    def test_sold_piece_on_another_order_is_not_changed(self):
        other = Customer.objects.create(source_id='r96-2', full_name='SAMPLE', customer_number='R96-2')
        self.save_new([self.chair], customer=other)
        self.edit(self.order, NEW_NAME, NEW_DESC)
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.name, OLD_NAME)

    def test_open_po_line_takes_the_new_text(self):
        po = self.po_for(self.order, self.chair)
        saved = self.edit(Document.objects.get(pk=self.order.pk), NEW_NAME, NEW_DESC)
        po_line = po.lines.get()
        self.assertEqual((po_line.item_name, po_line.description), (NEW_NAME, NEW_DESC))
        self.assertIn(f'{po.number} was changed to match (not received yet).', ' '.join(saved.notes_for_screen))
        self.assertEqual(PurchaseDocument.objects.get(pk=po.pk).version, po.version + 1)

    def test_po_line_with_its_own_description_is_kept(self):
        po = self.po_for(self.order, self.chair)
        po_line = po.lines.get(); po_line.description = 'SEND WITH THE SAMPLE CHAIRS'; po_line.save()
        saved = self.edit(Document.objects.get(pk=self.order.pk), NEW_NAME, NEW_DESC)
        po_line.refresh_from_db()
        self.assertEqual(po_line.description, 'SEND WITH THE SAMPLE CHAIRS')
        self.assertIn(f'{po.number} has its own description', ' '.join(saved.notes_for_screen))

    def test_received_po_is_not_changed_and_gets_a_note(self):
        po = self.po_for(self.order, self.chair)
        save_purchase(header(self.vendor), [dict(item=self.chair, quantity=D('6'), cost=D('140'), source_line=po.lines.get().pk)],
                      'voucher', 'post', po)
        saved = self.edit(Document.objects.get(pk=self.order.pk), NEW_NAME, NEW_DESC)
        po_line = po.lines.get()
        self.assertEqual(po_line.description, OLD_DESC)
        self.assertIn(f'This item is already on {po.number}; tell the vendor about the change.', saved.notes_for_screen)
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.name, NEW_NAME)   # the item itself still follows the order

    def test_same_name_note_only_when_another_item_has_it(self):
        Item.objects.create(sku='16799', name=NEW_NAME, description='SAMPLE CHAIR', price=1, on_hand=0, tax_rate=7)
        saved = self.edit(self.order, NEW_NAME, NEW_DESC)
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.name, NEW_NAME)   # a note, never a block
        self.assertIn(f'Item #16799 also uses the name {NEW_NAME}.', saved.notes_for_screen)

    def test_save_response_carries_the_note(self):
        from accounts.signin_helpers import sign_in_admin
        from sales.test_receipt_lock import SESSIONS
        data = document_data(self.order)
        data['lines'][0]['item_name'] = NEW_NAME
        data['action'] = 'save'
        with SESSIONS:
            sign_in_admin(self.client)
            response = self.client.post(f'/pos/order/{self.order.token}/', data=data, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['message'], 'Item #17055 in the Item List was changed to match this order.')
