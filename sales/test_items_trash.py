# SPDX-License-Identifier: AGPL-3.0-or-later
"""Items Trash: Delete Selected -> Trash -> Restore, and what keeps working while an item is in the Trash."""
import threading
import unittest
import uuid
from decimal import Decimal as D
from pathlib import Path

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from customers.models import Customer
from purchasing.models import Vendor, PurchaseDocument
from purchasing.services import save_purchase
from purchasing.tests import header, rows
from tags.models import FurnitureSet, FurnitureSetLine
from tags.sets import build, pdf_bytes
from . import trash
from .models import Document, InventoryAdjustment, Item, ItemTrash, StockMovement
from .services import WorkflowError, fulfill_order, save_document, save_order_payment

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin
SESSIONS = override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)


class TrashRulesTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='c1', first_name='Pat', last_name='Buyer', full_name='Pat Buyer')
        self.vendor = Vendor.objects.create(code='V', company='Vendor')
        self.chair = Item.objects.create(sku='CHAIR', name='Chair', description='Oak chair', price=100, on_hand=5, average_cost=40, order_cost=40, tax_rate=7, primary_vendor=self.vendor)
        self.table = Item.objects.create(sku='TABLE', name='Table', description='Oak table', price=500, on_hand=0, average_cost=200, order_cost=200, tax_rate=7, primary_vendor=self.vendor)
        self.unused = Item.objects.create(sku='LAMP', name='Lamp', description='Never sold', price=20, on_hand=None, tax_rate=7)

    def order(self, item=None, qty=2):
        item = item or self.chair
        return save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=item.pk, quantity=str(qty), price='100')]), 'order', uuid.uuid4())

    def test_trash_zeroes_stock_with_adjustment_and_restore_puts_it_back(self):
        entries = trash.trash([self.chair.pk, self.table.pk, self.unused.pk], by='Pat')
        self.assertEqual(len(entries), 3)
        self.chair.refresh_from_db(); self.table.refresh_from_db(); self.unused.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand, self.table.active, self.table.on_hand, self.unused.active, self.unused.on_hand), (False, 0, False, 0, False, None))
        adj = InventoryAdjustment.objects.get(item=self.chair)
        self.assertEqual((adj.old_quantity, adj.new_quantity, adj.old_cost, adj.new_cost, adj.note), (5, 0, 40, 40, 'Moved to Trash'))
        self.assertFalse(InventoryAdjustment.objects.filter(item=self.table).exists())  # nothing to zero
        entry = ItemTrash.objects.get(sku='CHAIR')
        self.assertEqual((entry.item, entry.item_name, entry.trashed_by, entry.quantity_zeroed), (self.chair, 'Chair', 'Pat', 5))
        self.assertIsNone(ItemTrash.objects.get(sku='TABLE').quantity_zeroed)
        # Trashing again is a no-op; restoring puts the quantity back and removes the entry.
        self.assertEqual(trash.trash([self.chair.pk]), [])
        restored = trash.restore([self.chair.pk, self.table.pk, self.unused.pk])
        self.assertEqual(len(restored), 3)
        self.chair.refresh_from_db(); self.unused.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand, self.unused.active, self.unused.on_hand), (True, 5, True, None))
        back = InventoryAdjustment.objects.filter(item=self.chair).order_by('pk').last()
        self.assertEqual((back.old_quantity, back.new_quantity, back.note), (0, 5, 'Restored from Trash'))
        self.assertEqual(InventoryAdjustment.objects.count(), 2)
        self.assertFalse(ItemTrash.objects.exists())

    def test_never_used_filter(self):
        self.order()
        self.assertEqual(list(trash.never_used(Item.objects.all())), [self.unused, self.table])
        FurnitureSet.objects.create(name='Set').lines.create(item=self.table, quantity=1)
        self.assertEqual(list(trash.never_used(Item.objects.all())), [self.unused])
        save_purchase(header(self.vendor), rows(self.unused, '1', '10'), 'po', 'save')
        self.assertEqual(list(trash.never_used(Item.objects.all())), [])

    def test_usage_lists_open_documents_and_sets(self):
        order = self.order()
        held = save_document(dict(action='hold', customer_id=self.customer.pk, lines=[dict(item_id=self.chair.pk, quantity='1', price='100')]), 'sale', uuid.uuid4())
        po = save_purchase(header(self.vendor), rows(self.chair, '3', '40'), 'po', 'save')
        tag_set = FurnitureSet.objects.create(name='Dining set'); tag_set.lines.create(item=self.chair, quantity=4)
        usage = trash.usage([self.chair, self.table])
        chair = usage[self.chair.pk]
        self.assertEqual((chair['orders'], chair['held'], chair['pos'], chair['sets'], chair['on_hand'], chair['warnings']), ([order], [held], [(po, D('3'))], [tag_set], 5, True))
        self.assertFalse(usage[self.table.pk]['warnings'])

    def test_open_order_with_trashed_item_saves_and_picks_up_but_refuses_new_lines(self):
        order = self.order()
        trash.trash([self.chair.pk])
        line = order.lines.get()
        # Re-save the open order: the existing line keeps its trashed item; quantity and price can still change.
        order = save_document(dict(action='save', version=order.version, customer_id=self.customer.pk, lines=[dict(id=line.pk, item_id=self.chair.pk, quantity='3', price='90')]), 'order', order.token)
        self.assertEqual((order.lines.get().quantity, order.lines.get().price, order.lines.get().item), (3, D('90'), self.chair))
        # A NEW line with the trashed item is refused, on this order or a new document.
        with self.assertRaisesMessage(WorkflowError, 'Chair is in the Items Trash'):
            save_document(dict(action='save', version=order.version, customer_id=self.customer.pk, lines=[dict(id=line.pk, item_id=self.chair.pk, quantity='3', price='90'), dict(item_id=self.chair.pk, quantity='1', price='90')]), 'order', order.token)
        with self.assertRaisesMessage(WorkflowError, 'Chair is in the Items Trash'):
            self.order()
        # Pickup of the trashed item still works (the item is on the order line) and takes the stock negative like any pickup.
        order.refresh_from_db()
        save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount='288.90', method='cash', reference=''))
        order.refresh_from_db()
        receipt = fulfill_order(order.pk, dict(version=order.version, token=str(uuid.uuid4()), **{f'qty_{line.pk}': '1'}))
        self.assertEqual((receipt.status, receipt.lines.get().item), ('completed', self.chair))
        self.chair.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand), (False, -1))
        # Restore adds the zeroed 5 back on top of what happened meanwhile.
        trash.restore([self.chair.pk]); self.chair.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand), (True, 4))

    def test_held_receipt_with_trashed_item_can_be_saved_and_completed(self):
        held = save_document(dict(action='hold', customer_id=self.customer.pk, lines=[dict(item_id=self.chair.pk, quantity='2', price='100')]), 'sale', uuid.uuid4())
        trash.trash([self.chair.pk])
        line = held.lines.get()
        held = save_document(dict(action='hold', version=held.version, customer_id=self.customer.pk, lines=[dict(id=line.pk, item_id=self.chair.pk, quantity='2', price='100')]), 'sale', held.token)
        done = save_document(dict(action='complete', version=held.version, customer_id=self.customer.pk, payment_method='cash', tendered='214', lines=[dict(id=line.pk, item_id=self.chair.pk, quantity='2', price='100')]), 'sale', held.token)
        self.assertEqual(done.status, 'completed')
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.on_hand, -2)
        self.assertEqual(StockMovement.objects.get(document=done).quantity, -2)

    def test_return_of_trashed_item_restocks_and_stays_in_trash(self):
        sale = save_document(dict(action='complete', customer_id=self.customer.pk, payment_method='cash', tendered='107', lines=[dict(item_id=self.chair.pk, quantity='1', price='100')]), 'sale', uuid.uuid4())
        trash.trash([self.chair.pk]); self.chair.refresh_from_db()
        self.assertEqual(self.chair.on_hand, 0)  # 4 zeroed
        ret = save_document(dict(action='complete', customer_id=self.customer.pk, payment_method='cash', tendered='-107', lines=[dict(returned_from_line=sale.lines.get().pk, quantity='-1', return_reason='other')]), 'sale', uuid.uuid4())
        self.assertEqual(ret.status, 'completed')
        self.chair.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand), (False, 1))
        trash.restore([self.chair.pk]); self.chair.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand), (True, 5))

    def test_po_with_trashed_item_saves_and_receives_but_refuses_new_po_lines(self):
        po = save_purchase(header(self.vendor), rows(self.chair, '3', '40'), 'po', 'save')
        trash.trash([self.chair.pk])
        po_line = po.lines.get()
        po = save_purchase(header(self.vendor, token=po.token, version=po.version), rows(self.chair, '4', '41', line_id=po_line.pk), 'po', 'save')
        self.assertEqual((po.lines.get().quantity, po.lines.get().cost), (4, D('41')))
        with self.assertRaisesMessage(WorkflowError, 'Chair is in the Items Trash'):
            save_purchase(header(self.vendor), rows(self.chair, '1', '40'), 'po', 'save')
        voucher = save_purchase(header(self.vendor), rows(self.chair, '2', '41', source_line=po_line.pk), 'voucher', 'post', source_po=po)
        self.assertEqual(voucher.status, 'posted')
        self.chair.refresh_from_db(); po.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand, po.lines.get().received), (False, 2, 2))
        with self.assertRaisesMessage(WorkflowError, 'Chair is in the Items Trash'):
            save_purchase(header(self.vendor), rows(self.chair, '1', '40'), 'voucher', 'post')
        trash.restore([self.chair.pk]); self.chair.refresh_from_db()
        self.assertEqual(self.chair.on_hand, 7)

    def test_furniture_set_with_trashed_item_warns_and_still_prints(self):
        tag_set = FurnitureSet.objects.create(name='Dining set'); tag_set.lines.create(item=self.chair, quantity=4)
        before = build(tag_set)
        trash.trash([self.chair.pk])
        after = build(tag_set)
        self.assertTrue(after['rows'][0]['inactive'])
        self.assertEqual((after['subtotal'], after['tax'], after['total']), (before['subtotal'], before['tax'], before['total']))
        self.assertTrue(pdf_bytes(tag_set).startswith(b'%PDF'))
        page = self.client.get(f'/tags/furniture-sets/{tag_set.token}/')
        self.assertContains(page, 'in the Items Trash')
        self.assertContains(self.client.get('/tags/'), '(in Items Trash)')
        # The set can be re-saved with its trashed line, but the trashed item can't be added to another set.
        self.assertEqual(self.client.post(f'/tags/furniture-sets/{tag_set.token}/', {'name': 'Dining set', 'tagline': '', 'copies': '2', 'item': [self.chair.pk], 'quantity': ['3']}).status_code, 302)
        self.assertEqual(FurnitureSetLine.objects.get(tag_set=tag_set).quantity, 3)
        other = self.client.post('/tags/furniture-sets/new/', {'name': 'Other', 'tagline': '', 'copies': '2', 'item': [self.chair.pk], 'quantity': ['1']})
        self.assertContains(other, 'Chair is in the Items Trash', status_code=400)
        self.assertEqual(self.client.get('/tags/furniture-sets/new/', {'item': [self.chair.pk, self.table.pk]}).context['lines'][0]['item'], self.table)

    def test_trashed_items_leave_pickers_but_stay_on_history(self):
        sale = save_document(dict(action='complete', customer_id=self.customer.pk, payment_method='cash', tendered='107', lines=[dict(item_id=self.chair.pk, quantity='1', price='100')]), 'sale', uuid.uuid4())
        trash.trash([self.chair.pk])
        self.assertEqual(self.client.get('/pos/lookup/items/?q=Chair').json()['results'], [])
        self.assertEqual(self.client.get('/purchasing/lookup/items/?q=Chair').json()['results'], [])
        self.assertEqual(self.client.get('/purchasing/item-picker/?q=Chair').json()['results'], [])
        self.assertNotContains(self.client.get('/purchasing/items/'), 'Oak chair')
        self.assertContains(self.client.get('/purchasing/items/?status=inactive'), 'Oak chair')
        self.assertContains(self.client.get('/purchasing/items/?status=unused'), 'Never sold')
        self.assertNotContains(self.client.get('/purchasing/items/?status=unused'), 'Oak chair')
        self.assertEqual(self.client.get('/pos/sale/new/', {'item': [self.chair.pk]}).context['initial']['lines'], [])
        self.assertContains(self.client.get(f'/pos/sale/{sale.token}/'), 'Chair')
        self.assertContains(self.client.get('/purchasing/trash/'), 'Oak chair')


@SESSIONS
class TrashViewTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='c1', first_name='Pat', last_name='Buyer', full_name='Pat Buyer')
        self.chair = Item.objects.create(sku='CHAIR', name='Chair', description='Oak chair', price=100, on_hand=5, average_cost=40, tax_rate=7)
        self.lamp = Item.objects.create(sku='LAMP', name='Lamp', description='Never sold', price=20, on_hand=0, tax_rate=7)

    def admin(self):
        sign_in_admin(self.client)  # Admin Mode comes only from an admin sign-in

    def test_delete_and_restore_need_admin_mode(self):
        self.assertEqual(self.client.get('/purchasing/items/delete/', {'item': self.chair.pk}).status_code, 302)
        self.assertEqual(self.client.post('/purchasing/items/delete/', {'item': self.chair.pk}).status_code, 403)
        self.assertEqual(self.client.post('/purchasing/trash/restore/', {'item': self.chair.pk}).status_code, 403)
        self.chair.refresh_from_db(); self.assertTrue(self.chair.active)
        page = self.client.get('/purchasing/items/')
        self.assertNotContains(page, 'Delete Selected')
        self.assertContains(page, 'Items Trash')
        self.assertNotContains(self.client.get('/purchasing/trash/'), 'Restore Selected')

    def test_delete_window_restore_screen_and_item_editor(self):
        self.admin()
        order = save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=self.chair.pk, quantity='2', price='100')]), 'order', uuid.uuid4())
        self.assertContains(self.client.get('/purchasing/items/'), 'Delete Selected')
        window = self.client.get('/purchasing/items/delete/', {'item': [self.chair.pk, self.lamp.pk], 'back': '/purchasing/items/?status=active'})
        self.assertContains(window, 'With warnings')
        self.assertContains(window, 'Quantity on hand 5 will be set to 0')
        self.assertContains(window, order.numbered_label)
        self.assertContains(window, 'Nothing attached (1)')
        self.assertContains(window, 'Move 2 Items to Trash')
        response = self.client.post('/purchasing/items/delete/', {'item': [self.chair.pk, self.lamp.pk]})
        self.assertRedirects(response, '/purchasing/trash/?moved=2')
        self.chair.refresh_from_db(); self.lamp.refresh_from_db()
        self.assertEqual((self.chair.active, self.chair.on_hand, self.lamp.active), (False, 0, False))
        self.assertEqual(ItemTrash.objects.get(sku='CHAIR').trashed_by, 'TEST Admin')
        page = self.client.get('/purchasing/trash/?moved=2')
        self.assertContains(page, '2 items moved to the Items Trash.')
        self.assertContains(page, 'Restore Selected')
        self.assertContains(page, 'Oak chair')
        self.assertContains(self.client.get('/purchasing/trash/?q=lamp'), 'Never sold')
        self.assertNotContains(self.client.get('/purchasing/trash/?q=lamp'), 'Oak chair')
        editor = self.client.get(f'/purchasing/items/{self.chair.pk}/')
        self.assertContains(editor, 'This item is in the Items Trash')
        self.assertContains(editor, 'Restore from Trash')
        self.assertContains(self.client.get(f'/purchasing/items/{self.chair.pk}/details/'), 'In Items Trash')
        # Editing a trashed item does not reactivate it (the form has no active checkbox any more).
        data = {'name': 'Chair', 'item_type': 'inventory', 'description': 'Oak chair, revised', 'price': '100', 'tax_rate': '7', 'active': 'on', 'version': Item.objects.get(pk=self.chair.pk).version}
        self.assertEqual(self.client.post(f'/purchasing/items/{self.chair.pk}/', data).status_code, 302)
        self.chair.refresh_from_db(); self.assertEqual((self.chair.active, self.chair.description), (False, 'Oak chair, revised'))
        response = self.client.post('/purchasing/trash/restore/', {'item': [self.chair.pk], 'back': f'/purchasing/items/{self.chair.pk}/'})
        self.assertRedirects(response, f'/purchasing/items/{self.chair.pk}/?restored=1')
        self.chair.refresh_from_db(); self.assertEqual((self.chair.active, self.chair.on_hand), (True, 5))
        self.assertContains(self.client.get(f'/purchasing/items/{self.chair.pk}/'), 'Move to Trash')
        self.assertContains(self.client.post('/purchasing/trash/restore/', {'item': [self.lamp.pk]}, follow=True), '1 item restored')
        self.assertFalse(ItemTrash.objects.exists())

    def test_delete_all_never_used_and_testing_delete_redirect(self):
        self.admin()
        save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=self.chair.pk, quantity='2', price='100')]), 'order', uuid.uuid4())
        self.assertContains(self.client.get('/purchasing/items/?status=unused'), 'Delete All Never-Used Items')
        window = self.client.get('/purchasing/items/delete/?all=unused')
        self.assertContains(window, 'Never sold')
        self.assertNotContains(window, 'Oak chair')
        self.assertRedirects(self.client.get(f'/records/items/{self.lamp.pk}/delete/'), f'/purchasing/items/delete/?item={self.lamp.pk}')
        self.assertEqual(self.client.get('/purchasing/items/delete/', {'item': 'x'}).status_code, 404)


class TrashRebuildTests(TestCase):
    """A rebuild from a QB backup reloads every item as active; the Items Trash is re-applied by item number."""

    @classmethod
    def setUpTestData(cls):
        import tempfile
        from .test_rebuild import write_fixture
        cls.dir = tempfile.mkdtemp(prefix='rebuild-trash-')
        cls.path = str(Path(cls.dir) / 'current.sqlite')
        write_fixture(cls.path)

    def test_rebuild_retrashes_by_sku_and_zeroes_reloaded_stock(self):
        from . import rebuild
        rebuild.rebuild_from_backup(self.path, commit=True)
        chair = Item.objects.get(sku='1001')
        self.assertEqual((chair.active, chair.on_hand), (True, 5))
        trash.trash([chair.pk], by='Pat')
        ItemTrash.objects.create(sku='NOT-IN-BACKUP', item_name='Gone')
        dry = rebuild.rebuild_from_backup(self.path, commit=False)
        self.assertEqual((dry['counts']['retrashed_items'], dry['counts']['trashed_skus_not_in_backup']), (1, 1))
        chair.refresh_from_db(); self.assertFalse(chair.active)  # dry run rolled back nothing of ours; still trashed
        result = rebuild.rebuild_from_backup(self.path, commit=True)
        self.assertEqual(result['counts']['retrashed_items'], 1)
        chair = Item.objects.get(sku='1001')
        self.assertEqual((chair.active, chair.on_hand), (False, 0))
        adj = InventoryAdjustment.objects.get(item=chair)
        self.assertEqual((adj.old_quantity, adj.new_quantity, adj.note), (5, 0, 'Moved to Trash'))
        entry = ItemTrash.objects.get(sku='1001')
        self.assertEqual((entry.item, entry.trashed_by, entry.quantity_zeroed, entry.value_zeroed), (chair, 'Pat', 5, D('200.60')))  # 5 × $40.12 (QB cost 40.123456, whole cents)
        self.assertIsNone(ItemTrash.objects.get(sku='NOT-IN-BACKUP').item)
        trash.restore([chair.pk]); chair.refresh_from_db()
        self.assertEqual((chair.active, chair.on_hand), (True, 5))


class TrashReviewFixTests(TestCase):
    """Cross-vendor review: the pickup exemption is server-internal, inventory value survives the
    Trash so Restore gives the never-trashed quantity AND average cost, and items already inactive when migration 0035
    runs are zeroed and recorded like any Trash."""

    def setUp(self):
        self.customer = Customer.objects.create(source_id='c1', first_name='Pat', last_name='Buyer', full_name='Pat Buyer')
        self.vendor = Vendor.objects.create(code='V', company='Vendor')

    def item(self, sku, on_hand=5, cost='40'):
        return Item.objects.create(sku=sku, name=sku.title(), description=f'{sku} desc', price=100, on_hand=on_hand, average_cost=D(cost), order_cost=D(cost), tax_rate=7, primary_vendor=self.vendor)

    def order(self, item, qty=1):
        return save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=item.pk, quantity=str(qty), price='100')]), 'order', uuid.uuid4())

    def pickup(self, order, qty=1):
        order.refresh_from_db(); line = order.lines.get()
        if order.total > order.paid:
            save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount=str(order.total - order.paid), method='cash', reference=''))
            order.refresh_from_db()
        return fulfill_order(order.pk, dict(version=order.version, token=str(uuid.uuid4()), **{f'qty_{line.pk}': str(qty)}))

    def po(self, item, qty, cost):
        """An open PO made BEFORE the item is trashed: receiving on its line is the only way stock can reach a trashed item."""
        return save_purchase(header(self.vendor), rows(item, qty, cost), 'po', 'save')

    def receive(self, item, qty, cost, po, **kw):
        return save_purchase(header(self.vendor, **kw), rows(item, qty, cost, source_line=po.lines.get().pk), 'voucher', 'post', source_po=po)

    def assertStock(self, item, on_hand, cost):
        item.refresh_from_db()
        self.assertEqual((item.on_hand, item.average_cost), (D(on_hand), D(cost)), item.sku)

    # --- 1. pickup_line is not a client capability ----------------------------------------------------------------

    def test_client_supplied_pickup_line_cannot_put_a_trashed_item_on_a_new_document(self):
        chair = self.item('CHAIR')
        order = self.order(chair)
        line = order.lines.get()
        trash.trash([chair.pk])
        for kind, action in [('order', 'save'), ('sale', 'hold'), ('sale', 'complete')]:
            with self.assertRaisesMessage(WorkflowError, 'Chair is in the Items Trash'):
                save_document(dict(action=action, customer_id=self.customer.pk, payment_method='cash', tendered='2140', lines=[dict(item_id=chair.pk, quantity='20', price='100', pickup_line=line.pk)]), kind, uuid.uuid4())
        # Nor as a new line on the order that holds it, nor through the register's JSON API.
        with self.assertRaisesMessage(WorkflowError, 'Chair is in the Items Trash'):
            save_document(dict(action='save', version=order.version, customer_id=self.customer.pk, lines=[dict(id=line.pk, item_id=chair.pk, quantity='1', price='100'), dict(item_id=chair.pk, quantity='1', price='100', pickup_line=line.pk)]), 'order', order.token)
        import json
        response = self.client.post('/pos/order/new/', json.dumps(dict(action='save', token=str(uuid.uuid4()), customer_id=self.customer.pk, lines=[dict(item_id=chair.pk, quantity='20', price='100', pickup_line=line.pk)])), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('is in the Items Trash', response.json()['error'])
        self.assertEqual((Document.objects.count(), order.lines.count()), (1, 1))

    def test_pickup_of_a_trashed_item_needs_the_order_itself_open_with_quantity_left(self):
        chair, table = self.item('CHAIR'), self.item('TABLE')
        order, cancelled = self.order(chair, 1), self.order(table, 1)
        trash.trash([chair.pk, table.pk])
        receipt = self.pickup(order)  # a genuine pickup still works
        self.assertEqual((receipt.status, receipt.source_order, receipt.lines.get().item, receipt.lines.get().source_order_line), ('completed', order, chair, order.lines.get()))
        order.refresh_from_db()
        with self.assertRaisesMessage(WorkflowError, 'Pickup quantity exceeds the remaining quantity.'):  # exhausted
            fulfill_order(order.pk, dict(version=order.version, token=str(uuid.uuid4()), **{f'qty_{order.lines.get().pk}': '1'}))
        save_document(dict(action='cancel', version=cancelled.version, customer_id=self.customer.pk, lines=[]), 'order', cancelled.token)
        cancelled.refresh_from_db()
        with self.assertRaisesMessage(WorkflowError, 'was cancelled. Nothing can be picked up on it.'):
            fulfill_order(cancelled.pk, dict(version=cancelled.version, token=str(uuid.uuid4()), **{f'qty_{cancelled.lines.get().pk}': '1'}))
        self.assertEqual(Document.objects.filter(kind='sale').count(), 1)

    # --- 2. inventory value is parked with the quantity -------------------------------------------------------------

    def test_receiving_editing_and_cancelling_vouchers_while_trashed_match_an_untrashed_control(self):
        control, trashed = self.item('CONTROL'), self.item('TRASHED')
        po_c, po_t = self.po(control, '4', '100'), self.po(trashed, '4', '100')
        entry, = trash.trash([trashed.pk])
        self.assertEqual((entry.quantity_zeroed, entry.value_zeroed), (5, D('200.00')))
        self.assertEqual(trash.parked(Item.objects.get(pk=trashed.pk)), 5)
        self.assertEqual(trash.parked(control), 0)
        vc, vt = self.receive(control, '2', '100', po_c), self.receive(trashed, '2', '100', po_t)
        self.assertStock(control, 7, '57.14')
        self.assertStock(trashed, 2, '57.14')  # 2 live + 5 parked, same blended cost as the control
        # a received voucher is corrected by reversing it and receiving again (no edit of a received voucher).
        # Reverse both, receive 3 @ $100 on both, then reverse those too.
        for item, voucher, po in [(control, vc, po_c), (trashed, vt, po_t)]:
            save_purchase(header(self.vendor, token=voucher.token, version=voucher.version), [], 'voucher', 'cancel', source_po=po)
        self.assertStock(control, 5, '40.00'); self.assertStock(trashed, 0, '40.00')
        vc, vt = self.receive(control, '3', '100', po_c), self.receive(trashed, '3', '100', po_t)
        self.assertStock(control, 8, '62.50'); self.assertStock(trashed, 3, '62.50')
        for item, voucher, po in [(control, vc, po_c), (trashed, vt, po_t)]:
            voucher.refresh_from_db()
            save_purchase(header(self.vendor, token=voucher.token, version=voucher.version), [], 'voucher', 'cancel', source_po=po)
        self.assertStock(control, 5, '40.00'); self.assertStock(trashed, 0, '40.00')
        self.receive(control, '2', '100', po_c); self.receive(trashed, '2', '100', po_t)
        trash.restore([trashed.pk])
        self.assertStock(control, 7, '57.14'); self.assertStock(trashed, 7, '57.14')
        back = InventoryAdjustment.objects.filter(item=trashed, note='Restored from Trash').get()
        self.assertEqual((back.old_quantity, back.new_quantity, back.old_cost, back.new_cost), (2, 7, D('57.14'), D('57.14')))
        self.assertEqual((control.order_cost, Item.objects.get(pk=trashed.pk).order_cost), (D('100.00'), D('100.00')))

    def test_sale_and_return_between_receiving_and_restore_match_the_control(self):
        control, trashed = self.item('CONTROL'), self.item('TRASHED')
        orders = {control.pk: self.order(control, 1), trashed.pk: self.order(trashed, 1)}
        po_c, po_t = self.po(control, '2', '100'), self.po(trashed, '2', '100')
        trash.trash([trashed.pk])
        self.receive(control, '2', '100', po_c); self.receive(trashed, '2', '100', po_t)
        receipts = {pk: self.pickup(order) for pk, order in orders.items()}
        self.assertStock(control, 6, '57.14'); self.assertStock(trashed, 1, '57.14')
        for pk, receipt in receipts.items():
            save_document(dict(action='complete', customer_id=self.customer.pk, payment_method='cash', tendered='-107', lines=[dict(returned_from_line=receipt.lines.get().pk, quantity='-1', return_reason='other')]), 'sale', uuid.uuid4())
        self.assertStock(control, 7, '57.14'); self.assertStock(trashed, 2, '57.14')
        self.assertFalse(Item.objects.get(pk=trashed.pk).active)
        trash.restore([trashed.pk])
        self.assertStock(trashed, 7, '57.14'); self.assertStock(control, 7, '57.14')

    def test_negative_stock_items_receive_and_restore_like_the_control(self):
        control, trashed = self.item('CONTROL', on_hand=-3), self.item('TRASHED', on_hand=-3)
        po_c, po_t = self.po(control, '4', '100'), self.po(trashed, '4', '100')
        entry, = trash.trash([trashed.pk])
        self.assertEqual((entry.quantity_zeroed, entry.value_zeroed), (-3, D('-120.00')))
        self.receive(control, '2', '100', po_c); self.receive(trashed, '2', '100', po_t)
        self.assertStock(control, -1, '100.00'); self.assertStock(trashed, 2, '100.00')
        self.receive(control, '2', '70', po_c); self.receive(trashed, '2', '70', po_t)
        self.assertStock(control, 1, '70.00'); self.assertStock(trashed, 4, '70.00')
        trash.restore([trashed.pk])
        self.assertStock(trashed, 1, '70.00')

    def test_unverified_stock_while_trashed_is_refused_like_the_control(self):
        control = Item.objects.create(sku='CONTROL', name='Control', description='d', price=100, on_hand=5, average_cost=None, tax_rate=7, primary_vendor=self.vendor)
        trashed = Item.objects.create(sku='TRASHED', name='Trashed', description='d', price=100, on_hand=5, average_cost=None, tax_rate=7, primary_vendor=self.vendor)
        po_c, po_t = self.po(control, '2', '100'), self.po(trashed, '2', '100')
        entry, = trash.trash([trashed.pk])
        self.assertEqual((entry.quantity_zeroed, entry.value_zeroed), (5, None))
        for item, po in [(control, po_c), (trashed, po_t)]:
            with self.assertRaisesMessage(WorkflowError, 'has stock but no average cost yet'):
                self.receive(item, '2', '100', po)

    # --- 5. migration 0035: items already inactive -----------------------------------------------------------------

    def test_migration_0035_zeroes_and_records_items_already_inactive(self):
        import importlib
        from django.apps import apps
        legacy = Item.objects.create(sku='LEGACY', name='Legacy', description='Old-style delete', on_hand=5, average_cost=40, active=False, version=3)
        Item.objects.create(sku='EMPTY', name='Empty', description='Nothing on hand', on_hand=0, active=False)
        importlib.import_module('sales.migrations.0035_item_trash').record_existing_inactive(apps, None)
        legacy.refresh_from_db()
        self.assertEqual((legacy.on_hand, legacy.active, legacy.version), (0, False, 4))
        adj = InventoryAdjustment.objects.get(item=legacy)
        self.assertEqual((adj.old_quantity, adj.new_quantity, adj.old_cost, adj.new_cost, adj.note), (5, 0, 40, 40, 'Moved to Trash'))
        entry = ItemTrash.objects.get(sku='LEGACY')
        self.assertEqual((entry.item, entry.quantity_zeroed, entry.value_zeroed, entry.trashed_by), (legacy, 5, D('200.00'), ''))
        self.assertEqual((ItemTrash.objects.get(sku='EMPTY').quantity_zeroed, InventoryAdjustment.objects.count()), (None, 1))
        trash.restore([legacy.pk]); legacy.refresh_from_db()
        self.assertEqual((legacy.active, legacy.on_hand, legacy.average_cost), (True, 5, 40))
        self.assertEqual(InventoryAdjustment.objects.filter(item=legacy, note='Restored from Trash').count(), 1)


class TrashReviewFix2Tests(TestCase):
    """Second cross-vendor review: a trashed item may stay only on the set lines it was already on,
    the set editor validates and writes under one item lock, and the guidance text names the admin sign-in."""

    def setUp(self):
        self.vendor = Vendor.objects.create(code='V', company='Vendor')

    def item(self, sku, on_hand=0):
        return Item.objects.create(sku=sku, name=sku.title(), description=f'{sku} desc', price=100, on_hand=on_hand, average_cost=D('40'), tax_rate=7, primary_vendor=self.vendor)

    def save(self, tag_set, items, quantities, name='Dining set'):
        url = f'/tags/furniture-sets/{tag_set.token}/' if tag_set else '/tags/furniture-sets/new/'
        return self.client.post(url, dict(name=name, tagline='', copies='1', item=[str(i.pk) for i in items], quantity=quantities))

    def lines(self, tag_set):
        return [(l.item_id, l.quantity) for l in tag_set.lines.all()]

    def test_tag_set_keeps_a_trashed_line_but_refuses_a_new_or_second_one(self):
        t, x = self.item('TRASHED'), self.item('LIVE')
        tag_set = FurnitureSet.objects.create(name='Dining set'); tag_set.lines.create(item=t, quantity=1); tag_set.lines.create(item=x, quantity=2)
        trash.trash([t.pk])
        # The saved line may stay (reordered, another quantity); the live item is free.
        self.assertEqual(self.save(tag_set, [x, t], ['2', '3']).status_code, 302)
        self.assertEqual(self.lines(tag_set), [(x.pk, 2), (t.pk, 3)])
        # A second line with the trashed item is a NEW line: refused, nothing written.
        response = self.save(tag_set, [t, t], ['1', '2'])
        self.assertContains(response, 'Trashed is in the Items Trash. Restore it (Items Trash, admin sign-in) before adding it to a set.', status_code=400)
        self.assertEqual(self.lines(tag_set), [(x.pk, 2), (t.pk, 3)])
        # A new set can't take it at all.
        self.assertEqual(self.save(None, [t], ['1']).status_code, 400)
        self.assertEqual(FurnitureSet.objects.count(), 1)
        # Once its line is removed and the set saved, the trashed item can't come back.
        self.assertEqual(self.save(tag_set, [x], ['2']).status_code, 302)
        self.assertEqual(self.save(tag_set, [x, t], ['2', '1']).status_code, 400)
        self.assertEqual(self.lines(tag_set), [(x.pk, 2)])
        # A set that legitimately holds the same trashed item on two saved lines keeps both, not three.
        tag_set.lines.create(item=t, quantity=1); tag_set.lines.create(item=t, quantity=1)
        self.assertEqual(self.save(tag_set, [x, t, t], ['2', '1', '1']).status_code, 302)
        self.assertEqual(self.save(tag_set, [t, t, t], ['1', '1', '1']).status_code, 400)
        self.assertEqual(self.lines(tag_set), [(x.pk, 2), (t.pk, 1), (t.pk, 1)])

    def test_set_editor_refuses_in_one_transaction_with_nothing_half_written(self):
        t, x = self.item('TRASHED'), self.item('LIVE')
        tag_set = FurnitureSet.objects.create(name='Dining set', tagline='old'); tag_set.lines.create(item=x, quantity=2)
        trash.trash([t.pk])
        # Name/message errors are found inside the same transaction, after the rows parsed: the editor shows the rows again.
        response = self.save(tag_set, [x], ['5'], name='')
        self.assertContains(response, 'Enter a name for this set', status_code=400)
        self.assertEqual(response.context['lines'][0]['quantity'], D('5'))
        tag_set.refresh_from_db()
        self.assertEqual((tag_set.name, tag_set.tagline, self.lines(tag_set)), ('Dining set', 'old', [(x.pk, 2)]))

    @SESSIONS
    def test_trash_screens_point_at_the_admin_sign_in(self):
        t = self.item('TRASHED')
        trash.trash([t.pk])
        for url in ['/purchasing/trash/', f'/purchasing/items/{t.pk}/']:
            page = self.client.get(url)
            self.assertContains(page, 'Sign in as an admin to restore')
            self.assertNotContains(page, 'Admin Mode (File menu)')
            self.assertNotContains(page, 'Turn on Admin Mode')
        self.assertContains(self.client.get('/purchasing/trash/?q=nothing-here'), 'Delete Selected, admin sign-in')

    def test_migration_0035_reverse_puts_parked_stock_back_and_leaves_the_item_inactive(self):
        import importlib
        from django.apps import apps
        migration = importlib.import_module('sales.migrations.0035_item_trash')
        legacy = self.item('LEGACY', on_hand=5); gone = self.item('GONE', on_hand=0); live = self.item('LIVE', on_hand=9)
        trash.trash([legacy.pk, gone.pk, live.pk])
        trash.restore([live.pk])
        Item.objects.filter(pk=legacy.pk).update(on_hand=1)   # a return restocked it while trashed (decision 6)
        ItemTrash.objects.filter(sku='GONE').update(item=None, quantity_zeroed=2)  # trashed item number no longer in the data
        gone.delete()
        migration.put_parked_stock_back(apps, None)
        legacy.refresh_from_db(); live.refresh_from_db()
        self.assertEqual((legacy.on_hand, legacy.active, legacy.version), (6, False, 3))
        self.assertEqual((live.on_hand, live.active), (9, True))
        adj = InventoryAdjustment.objects.filter(item=legacy).order_by('pk').values_list('old_quantity', 'new_quantity', 'note')
        self.assertEqual(list(adj), [(5, 0, 'Moved to Trash'), (1, 6, 'Restored from Trash')])
        self.assertEqual(InventoryAdjustment.objects.filter(item=live).count(), 2)


class Migration0035DrillTests(TransactionTestCase):
    """Finding 2 of the second review: the real migration forward -> reverse -> forward, with historical models."""

    BEFORE, AFTER = ('sales', '0034_remove_line_receipt_base_price'), ('sales', '0035_item_trash')

    def migrate(self, target):
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        executor.migrate([target] if isinstance(target, tuple) else target)
        return executor.loader.project_state([target] if isinstance(target, tuple) else target).apps

    def test_reverse_restores_the_zeroed_stock_before_dropping_the_table(self):
        from django.db.migrations.executor import MigrationExecutor
        old = self.migrate(self.BEFORE)
        pk = old.get_model('sales', 'Item').objects.create(sku='LEGACY', name='Legacy', active=False, on_hand=5, average_cost=40).pk
        try:
            new = self.migrate(self.AFTER)
            NewItem, NewTrash, NewAdj = (new.get_model('sales', name) for name in ('Item', 'ItemTrash', 'InventoryAdjustment'))
            self.assertEqual((NewItem.objects.get(pk=pk).on_hand, NewTrash.objects.get(sku='LEGACY').quantity_zeroed), (0, 5))
            NewItem.objects.filter(pk=pk).update(on_hand=1)  # restocked by a return while in the Trash
            old = self.migrate(self.BEFORE)
            item = old.get_model('sales', 'Item').objects.get(pk=pk)
            self.assertEqual((item.on_hand, item.active), (6, False))
            self.assertNotIn('sales_itemtrash', connection.introspection.table_names())
            notes = list(old.get_model('sales', 'InventoryAdjustment').objects.filter(item_id=pk).order_by('pk').values_list('old_quantity', 'new_quantity', 'note'))
            self.assertEqual(notes, [(5, 0, 'Moved to Trash'), (1, 6, 'Restored from Trash')])
            new = self.migrate(self.AFTER)
            self.assertEqual((new.get_model('sales', 'Item').objects.get(pk=pk).on_hand, new.get_model('sales', 'ItemTrash').objects.get(sku='LEGACY').quantity_zeroed), (0, 6))
        finally:
            self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@unittest.skipUnless(connection.vendor == 'postgresql', 'row locks are real only on PostgreSQL')
class TagSetTrashRaceTests(TransactionTestCase):
    """Finding 1 of the second review: a Delete that lands while a set is being saved is seen by the save (the set
    editor reads the Item rows FOR UPDATE in the same transaction that writes the lines)."""

    def test_delete_landing_during_a_set_save_is_seen(self):
        vendor = Vendor.objects.create(code='V', company='Vendor')
        item = Item.objects.create(sku='RACE', name='Race', description='d', price=100, on_hand=0, average_cost=D('40'), tax_rate=7, primary_vendor=vendor)
        locked, go = threading.Event(), threading.Event()

        def delete_on_another_connection():
            try:
                with transaction.atomic():
                    trash.trash([item.pk])   # holds the Item row lock until `go`
                    locked.set()
                    go.wait(10)
            finally:
                connection.close()
        deleter = threading.Thread(target=delete_on_another_connection); deleter.start()
        self.assertTrue(locked.wait(10))
        threading.Timer(0.5, go.set).start()  # the Delete commits half a second after the save started waiting on it
        response = self.client.post('/tags/furniture-sets/new/', dict(name='Race set', tagline='', copies='1', item=[str(item.pk)], quantity=['1']))
        deleter.join(10)
        self.assertContains(response, 'is in the Items Trash', status_code=400)
        self.assertEqual(FurnitureSet.objects.count(), 0)
        item.refresh_from_db(); self.assertFalse(item.active)
