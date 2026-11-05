# SPDX-License-Identifier: AGPL-3.0-or-later
"""the Awaiting Pickup screen. Paid sales held for pickup and open sales orders with goods
in the store; Mark Picked Up from the list; the Navigator tile under Sales History."""
import re
import uuid
from datetime import date, timedelta
from unittest import mock
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_register
from customers.models import Customer
from purchasing.models import Department, PurchaseDocument, PurchaseLine, Vendor

from .awaiting import awaiting_receipts, ready_orders
from .models import Document, Item, Line

URL = '/pos/awaiting-pickup/'


def ago(days):
    return timezone.now() - timedelta(days=days)


class AwaitingBase(TestCase):
    def setUp(self):
        # only app vouchers posted on/after go-live count; these tests post vouchers days ago, so move go-live back.
        patcher = mock.patch('sales.awaiting.GO_LIVE', date(2000, 1, 1))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.customer = Customer.objects.create(source_id='r101', full_name='TEST AWAITING', phone='518-555-0101')
        self.vendor = Vendor.objects.create(code='TST', company='TEST VENDOR')
        self.special = Department.objects.create(code='SPE', name='SPECIAL ORDER')
        self.stool = Item.objects.create(sku='20001', name='TEST STOOL', description='TEST SEELY COUNTER STOOL', price='100.00',
                                         on_hand='0', tax_rate='7.000', average_cost='40.00')
        self.table = Item.objects.create(sku='20002', name='TEST TABLE', description='TEST DINING TABLE', price='900.00',
                                         on_hand='0', tax_rate='7.000', average_cost='400.00')

    # --- receipts ---
    def receipt(self, held=True, est=None, sold_days_ago=1, picked=False, status='completed', notes=''):
        doc = Document.objects.create(kind='sale', status=status, customer=self.customer, customer_name=self.customer.full_name,
                                      hold_for_pickup=held, est_pickup_date=est, completed_at=ago(sold_days_ago),
                                      picked_up_at=ago(0) if picked else None, total=D('214.00'), cashier='TEST CASHIER',
                                      notes=notes)
        Line.objects.create(document=doc, position=0, item=self.stool, sku=self.stool.sku, item_name=self.stool.name,
                            description=self.stool.description, quantity=2, price='100.00', tax_rate='7.000')
        return doc

    # --- orders ---
    def order(self, *lines, days_ago=30, status='open', imported=False, deposit=None):
        order = Document.objects.create(kind='order', status=status, customer=self.customer, customer_name=self.customer.full_name,
                                        source_id=f'qb-so-{uuid.uuid4()}' if imported else None, total=D('1000.00'),
                                        deposit_taken=deposit)
        Document.objects.filter(pk=order.pk).update(created_at=ago(days_ago))
        order.refresh_from_db()
        made = []
        for i, (item, qty) in enumerate(lines):
            made.append(Line.objects.create(document=order, position=i, item=item, sku=item.sku if item else 'GONE',
                                            item_name=item.name if item else 'GONE SOLD',
                                            description=item.description if item else '(SOLD TEST) GONE PIECE',
                                            quantity=qty, price='100.00', tax_rate='7.000'))
        return order, made

    def po_for(self, line, qty=None, received=0, item=None, sku=None, linked=True):
        po = PurchaseDocument.objects.create(kind='po', status='open', vendor=self.vendor, vendor_name=self.vendor.company)
        item = item if item is not None else line.item
        return PurchaseLine.objects.create(document=po, position=0, item=item, sku=sku or (item.sku if item else line.sku),
                                           item_name='x', description='x', quantity=qty or line.quantity, cost='10.00',
                                           received=received, sales_line=line if linked else None)

    def receive(self, po_line, qty, days_ago=3, imported=False):
        voucher = PurchaseDocument.objects.create(kind='voucher', status='posted', vendor=self.vendor, vendor_name=self.vendor.company,
                                                  source_po=po_line.document, posted_at=ago(days_ago),
                                                  source_id=f'qb-rv-{uuid.uuid4()}' if imported else None)
        PurchaseLine.objects.create(document=voucher, position=0, item=po_line.item, sku=po_line.sku, item_name='x', description='x',
                                    quantity=qty, cost='10.00', source_line=po_line)
        PurchaseLine.objects.filter(pk=po_line.pk).update(received=po_line.received + qty)
        po_line.refresh_from_db()
        return voucher

    def pickup(self, line, qty, status='completed'):
        receipt = Document.objects.create(kind='sale', status=status, customer=self.customer, customer_name=self.customer.full_name,
                                          source_order=line.document, completed_at=ago(0))
        Line.objects.create(document=receipt, position=0, item=line.item, sku=line.sku, item_name=line.item_name,
                            description=line.description, quantity=qty, price='100.00', tax_rate='7.000', source_order_line=line)
        return receipt

    def orders(self):
        return {r['doc'].pk: r for r in ready_orders()}


class AwaitingReceiptTests(AwaitingBase):
    def test_only_receipts_still_awaiting_pickup_are_listed(self):
        waiting = self.receipt()
        self.receipt(held=False)
        self.receipt(picked=True)
        self.receipt(status='cancelled')
        self.receipt(status='held')
        self.assertEqual([r['doc'] for r in awaiting_receipts()], [waiting])

    def test_sorted_by_est_pickup_date_no_date_last_then_date_sold(self):
        today = timezone.localdate()
        none_old = self.receipt(est=None, sold_days_ago=9)
        future = self.receipt(est=today + timedelta(days=5))
        past = self.receipt(est=today - timedelta(days=2), sold_days_ago=4)
        none_new = self.receipt(est=None, sold_days_ago=1)
        self.assertEqual([r['doc'] for r in awaiting_receipts()], [past, future, none_old, none_new])

    def test_row_text(self):
        doc = self.receipt(notes='Car was full, back Friday with a truck')
        row = awaiting_receipts()[0]
        self.assertEqual(row['items'], '2 × TEST SEELY COUNTER STOOL')
        self.assertEqual(row['phone'], '518-555-0101')
        self.assertEqual(row['days'], 1)
        page = self.client.get(URL).content.decode()
        self.assertIn(f'href="/pos/sale/{doc.token}/"', page)
        self.assertIn('Car was full, back Friday with a truck', page)
        self.assertIn('No date', page)

    def test_mark_picked_up_from_the_list_removes_the_row(self):
        doc = self.receipt()
        page = self.client.get(URL).content.decode()
        self.assertIn('Mark Picked Up', page)
        self.assertIn('data-confirm="Mark Receipt', page)
        response = self.client.post(f'/pos/sale/{doc.token}/pickup/', dict(action='picked_up', version=doc.version, next='awaiting'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(URL + '?done='))
        doc.refresh_from_db()
        self.assertIsNotNone(doc.picked_up_at)
        page = self.client.get(response['Location']).content.decode()
        self.assertIn('marked picked up', page)
        self.assertNotIn(f'/pos/sale/{doc.token}/', page)
        self.assertIn('Nothing is waiting for pickup.', page)

    def test_mark_picked_up_error_returns_to_the_list(self):
        doc = self.receipt(picked=True)
        response = self.client.post(f'/pos/sale/{doc.token}/pickup/', dict(action='picked_up', version=doc.version, next='awaiting'))
        self.assertTrue(response['Location'].startswith(URL + '?error='))

    def test_receipt_screen_mark_picked_up_unchanged(self):
        doc = self.receipt()
        response = self.client.post(f'/pos/sale/{doc.token}/pickup/', dict(action='picked_up', version=doc.version))
        self.assertEqual(response['Location'], f'/pos/sale/{doc.token}/?saved=pickup-done')


class ReadyOrderTests(AwaitingBase):
    def test_line_received_on_a_linked_po_is_ready(self):
        order, (line,) = self.order((self.table, 1))
        po_line = self.po_for(line)
        self.assertNotIn(order.pk, self.orders())          # ordered, not in yet
        voucher = self.receive(po_line, 1, days_ago=3)
        row = self.orders()[order.pk]
        self.assertEqual(row['ready'], '1 of 1')
        self.assertEqual(row['came_in'], timezone.localdate(voucher.posted_at))
        self.assertEqual(row['days'], 3)
        self.assertEqual(row['items'], '1 × TEST DINING TABLE')

    def test_partly_received_order_shows_x_of_y(self):
        order, (stools, table) = self.order((self.stool, 2), (self.table, 1))
        self.receive(self.po_for(stools), 2)
        self.po_for(table)
        row = self.orders()[order.pk]
        self.assertEqual(row['ready'], '2 of 3')
        self.assertEqual(row['items'], '2 × TEST SEELY COUNTER STOOL')
        page = self.client.get(URL).content.decode()
        self.assertIn('2 of 3', page)
        self.assertIn(f'href="/pos/order/{order.token}/"', page)

    def test_partly_received_line(self):
        order, (stools,) = self.order((self.stool, 4))
        self.receive(self.po_for(stools), 1)
        self.assertEqual(self.orders()[order.pk]['ready'], '1 of 4')

    def test_cancelled_and_completed_orders_are_excluded(self):
        for status in ('cancelled', 'completed'):
            order, (line,) = self.order((self.table, 1), status=status)
            self.receive(self.po_for(line), 1)
            self.assertNotIn(order.pk, self.orders())

    def test_fully_picked_up_order_is_excluded(self):
        order, (line,) = self.order((self.table, 1))
        self.receive(self.po_for(line), 1)
        self.pickup(line, 1)
        self.assertNotIn(order.pk, self.orders())

    def test_picked_up_part_is_not_ready_again(self):
        order, (line,) = self.order((self.stool, 3))
        self.receive(self.po_for(line), 2)
        self.pickup(line, 2)
        self.assertNotIn(order.pk, self.orders())         # the 2 that came in went home; 1 still on order
        self.pickup(line, 1, status='cancelled')           # a cancelled pickup doesn't count
        self.receive(PurchaseLine.objects.get(sales_line=line), 1)
        self.assertEqual(self.orders()[order.pk]['ready'], '1 of 1')

    def test_in_stock_line_is_not_listed(self):
        """goods on the shelf were never received for the order, so they don't count."""
        Item.objects.filter(pk=self.stool.pk).update(on_hand=2)
        older, _ = self.order((self.stool, 2), days_ago=20)
        self.assertNotIn(older.pk, self.orders())

    def test_qb_special_order_piece_matched_by_item_number(self):
        # QB never linked its POs to orders, and deleted some SOLD items after they came in (no item, QB item number).
        order, (line,) = self.order((None, 1), imported=True, deposit=D('300.00'))
        po_line = self.po_for(line, item=None, sku='GONE', linked=False)
        self.assertNotIn(order.pk, self.orders())
        self.receive(po_line, 1, days_ago=10)
        row = self.orders()[order.pk]
        self.assertEqual(row['ready'], '1 of 1')
        self.assertEqual(row['paid'], D('300.00'))
        self.assertEqual(row['balance'], D('700.00'))

    def test_qb_sold_item_matched_by_item(self):
        chest = Item.objects.create(sku='20003', name='TEST CHEST SOLD', description='(SOLD TEST) CHEST', price='500.00',
                                    on_hand='0', tax_rate='7.000', department=self.special)
        order, (line,) = self.order((chest, 1), imported=True)
        self.receive(self.po_for(line, linked=False), 1)
        self.assertEqual(self.orders()[order.pk]['ready'], '1 of 1')

    def test_sorted_oldest_came_in_first(self):
        a, (la,) = self.order((self.table, 1))
        b, (lb,) = self.order((self.table, 1))
        self.receive(self.po_for(la), 1, days_ago=2)
        self.receive(self.po_for(lb), 1, days_ago=9)
        self.assertEqual([r['doc'] for r in ready_orders()], [b, a])


class PageTests(AwaitingBase):
    def test_no_date_filter(self):
        """the app-voucher rule keeps the QuickBooks backlog off the page, so the Show dropdown is gone."""
        old, (lo,) = self.order((self.table, 1), days_ago=900)
        self.receive(self.po_for(lo), 1, days_ago=400)
        page = self.client.get(URL).content.decode()
        self.assertIn(f'/pos/order/{old.token}/', page)
        self.assertNotIn('id="awaiting-since"', page)
        self.assertNotIn('older order', page)

    def test_empty_states(self):
        page = self.client.get(URL).content.decode()
        self.assertEqual(page.count('Nothing is waiting for pickup.'), 2)

    @override_settings(MIDDLEWARE=AUTH_MIDDLEWARE + ['accounts.middleware.RequireLoginMiddleware'], POS_REQUIRE_LOGIN=True)
    def test_register_login_can_see_it(self):
        self.assertEqual(self.client.get(URL).status_code, 302)       # signed out: to the login page
        sign_in_register(self.client)
        self.assertEqual(self.client.get(URL).status_code, 200)
        self.assertEqual(self.client.get('/pos/held/').status_code, 200)

    def test_navigator_tile_between_held_receipts_and_tag_generator(self):
        page = self.client.get('/').content.decode()
        labels = re.findall(r'<span class="task-label">([^<]+)</span>', page)
        i = labels.index('Awaiting Pickup')
        self.assertEqual(labels[i - 1:i + 2], ['Held Receipts', 'Awaiting Pickup', 'Tag Generator'])
        self.assertIn('href="/pos/awaiting-pickup/"', page)
        self.assertIn('<symbol id="awaiting-pickup"', page)
        tile = re.search(r'<div class="task ([^"]*)">\s*<a href="/pos/awaiting-pickup/">', page)
        self.assertEqual(tile.group(1).strip(), '')                  # no flow arrows, like the other row-2 tiles
