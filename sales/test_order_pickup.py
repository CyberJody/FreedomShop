# SPDX-License-Identifier: AGPL-3.0-or-later
""""Hold for Pickup" on sales orders, and the Awaiting Pickup page's order list counting only goods
received in this app. The box is ticked by default (new, app-made and imported orders), staff untick it
for third-party delivery, and it changes no money, tax or stock."""
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from purchasing.models import Department, PurchaseDocument, PurchaseLine, Vendor
from sales import awaiting
from sales.awaiting import ready_orders
from sales.models import Document, Item, Line, StockMovement
from sales.services import WorkflowError, save_document, save_order_payment
from sales.test_imported_orders import ImportedOrderBase
from sales.views import document_data

NY = ZoneInfo('America/New_York')
URL = '/pos/awaiting-pickup/'


def box(page):
    """The order screen's Hold for Pickup checkbox tag, or None."""
    m = re.search(r'<input type="checkbox" id="order-pickup"[^>]*>', page)
    return m.group(0) if m else None


def money_state(order):
    order = Document.objects.get(pk=order.pk)
    return dict(subtotal=order.subtotal, discount=order.discount, tax=order.tax, total=order.total, paid=order.paid,
                balance=order.balance_due, status=order.status, tax_exempt=order.tax_exempt,
                lines=list(order.lines.order_by('pk').values_list('quantity', 'price', 'tax_rate', 'item_id')),
                payments=list(order.payments.order_by('pk').values()),       # amounts and their locked tax
                stock=StockMovement.objects.count(),
                overrides=order.overrides.count())


class OrderPickupFieldTests(ImportedOrderBase):
    """The box on the order screen and its save. ImportedOrderBase: an open QuickBooks order with a QB deposit."""

    def new_app_order(self, **extra):
        token = str(uuid.uuid4())
        data = dict(action='save', version=0, token=token, customer_id=self.customer.pk,
                    lines=[dict(item_id=self.item.pk, quantity='1', price='100.00')], **extra)
        return save_document(data, 'order', token)

    def test_default_ticked_new_app_and_imported_orders(self):
        self.assertTrue(self.order.order_pickup)                       # imported open QB order (migration default)
        self.assertTrue(self.new_app_order().order_pickup)             # saved without the box (any other path)
        self.assertTrue(self.new_app_order(order_pickup=True).order_pickup)
        self.assertFalse(self.new_app_order(order_pickup=False).order_pickup)
        page = self.client.get('/pos/order/new/').content.decode()
        self.assertIn('checked', box(page))
        self.assertNotIn('disabled', box(page))
        self.assertIn('Hold for Pickup</span></label>', page)
        # Same row as Tax-exempt.
        row = page[page.index('<div class="check-row no-print">'):]
        self.assertLess(row.index('id="tax-exempt"'), row.index('id="order-pickup"'))
        self.assertLess(row.index('id="order-pickup"'), row.index('</div>'))

    def test_receipt_screen_has_no_order_box_and_keeps_its_own(self):
        page = self.client.get('/pos/sale/new/').content.decode()
        self.assertIsNone(box(page))
        self.assertIn('id="hold-for-pickup"', page)
        token = str(uuid.uuid4())
        sale = save_document(dict(action='complete', version=0, token=token, customer_id=self.customer.pk, order_pickup=False,
                                  lines=[dict(item_id=self.item.pk, quantity='1', price='100.00')],
                                  payments=[dict(method='cash', amount='107.00')]), 'sale', token)
        self.assertFalse(sale.hold_for_pickup)                         # 's receipt default stays False
        self.assertTrue(sale.order_pickup)                             # ignored on receipts

    def test_untick_and_tick_on_imported_order_a_later_day_changes_no_money_tax_or_stock(self):
        self.pay('20.00')                                              # an app deposit on top of QB's
        later = timezone.now() + timedelta(days=9)
        before = money_state(self.order)
        with mock.patch('django.utils.timezone.now', return_value=later), \
                mock.patch('sales.models.store_today', return_value=timezone.localdate(later)):
            page = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
            self.assertIn('checked', box(page))
            self.assertNotIn('disabled', box(page))
            self.assertFalse('id="locked-notice"' in page or 'id="admin-override"' in page)   # no lock, no override
            self.save(order_pickup=False)
            self.order.refresh_from_db()
            self.assertFalse(self.order.order_pickup)
            self.assertNotIn('checked', box(self.client.get(f'/pos/order/{self.order.token}/').content.decode()))
            self.assertEqual(money_state(self.order), before)
            self.save(order_pickup=True)
        self.order.refresh_from_db()
        self.assertTrue(self.order.order_pickup)
        self.assertEqual(money_state(self.order), before)
        self.assertTrue(self.order.from_quickbooks)                     # still QB's recorded figures
        self.assertEqual(self.order.total, D('214'))

    def test_app_order_with_deposit_later_day(self):
        order = self.new_app_order()
        self.order = order
        self.pay('50.00')
        before = money_state(order)
        later = timezone.now() + timedelta(days=30)
        with mock.patch('django.utils.timezone.now', return_value=later), \
                mock.patch('sales.models.store_today', return_value=timezone.localdate(later)):
            order.refresh_from_db()
            rows = document_data(order)['lines']
            save_document(dict(action='save', version=order.version, token=str(order.token), customer_id=self.customer.pk,
                               lines=rows, order_pickup=False), 'order', order.token)
        order.refresh_from_db()
        self.assertFalse(order.order_pickup)
        self.assertEqual(money_state(order), before)

    def test_bad_value_refused(self):
        with self.assertRaises(WorkflowError):
            self.save(order_pickup='no')

    def test_read_only_on_cancelled_and_completed_orders(self):
        cancelled = Document.objects.create(kind='order', status='cancelled', customer=self.customer, customer_name='X')
        completed = Document.objects.create(kind='order', status='completed', customer=self.customer, customer_name='X')
        closed_qb = Document.objects.create(kind='order', status='completed', source_id='QBSO-closed', customer=self.customer,
                                            customer_name='X')
        for doc in (cancelled, completed, closed_qb):
            page = self.client.get(f'/pos/order/{doc.token}/').content.decode()
            self.assertIn('disabled', box(page), doc.status)
        with self.assertRaises(WorkflowError):
            save_document(dict(action='save', version=cancelled.version, token=str(cancelled.token), customer_id=self.customer.pk,
                               lines=[], order_pickup=False), 'order', cancelled.token)
        # A completed order: the box is ignored even if a request sends it.
        Line.objects.create(document=completed, position=0, item=self.item, sku='CHAIR', item_name='Chair', description='Oak chair',
                            quantity=1, price=D('100'), tax_rate=D('7'))
        try:
            save_document(dict(action='save', version=completed.version, token=str(completed.token), customer_id=self.customer.pk,
                               lines=[dict(item_id=self.item.pk, quantity='1', price='100.00')], order_pickup=False), 'order', completed.token)
        except WorkflowError:
            pass
        self.assertTrue(Document.objects.get(pk=completed.pk).order_pickup)


class AppReceivedRuleTests(TestCase):
    """Only goods received on a voucher made in this app, posted on/after go-live, make an order ready."""

    def setUp(self):
        from customers.models import Customer
        self.customer = Customer.objects.create(source_id='r102', full_name='TEST R102', phone='518-555-0102')
        self.vendor = Vendor.objects.create(code='TST', company='TEST VENDOR')
        self.special = Department.objects.create(code='SPE', name='SPECIAL ORDER')
        self.table = Item.objects.create(sku='30002', name='TEST TABLE', description='TEST DINING TABLE', price='900.00',
                                         on_hand='0', tax_rate='7.000')
        self.now = timezone.now()

    def order(self, item, qty=1, imported=False, days_ago=30, pickup=True):
        order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name=self.customer.full_name,
                                        source_id=f'qb-so-{uuid.uuid4()}' if imported else None, total=D('963.00'),
                                        order_pickup=pickup)
        Document.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=days_ago))
        order.refresh_from_db()
        line = Line.objects.create(document=order, position=0, item=item, sku=item.sku, item_name=item.name,
                                   description=item.description, quantity=qty, price='900.00', tax_rate='7.000')
        return order, line

    def po(self, line, linked=True):
        po = PurchaseDocument.objects.create(kind='po', status='open', vendor=self.vendor, vendor_name=self.vendor.company)
        return PurchaseLine.objects.create(document=po, position=0, item=line.item, sku=line.sku, item_name='x', description='x',
                                           quantity=line.quantity, cost='10.00', sales_line=line if linked else None)

    def receive(self, po_line, qty, when, imported=False):
        voucher = PurchaseDocument.objects.create(kind='voucher', status='posted', vendor=self.vendor, vendor_name='TEST VENDOR',
                                                  source_po=po_line.document, posted_at=when,
                                                  source_id=f'qb-rv-{uuid.uuid4()}' if imported else None)
        PurchaseLine.objects.create(document=voucher, position=0, item=po_line.item, sku=po_line.sku, item_name='x',
                                    description='x', quantity=qty, cost='10.00', source_line=po_line)
        PurchaseLine.objects.filter(pk=po_line.pk).update(received=po_line.received + qty)
        po_line.refresh_from_db()
        return voucher

    def rows(self):
        return {r['doc'].pk: r for r in ready_orders()}

    def after_go_live(self, days=1):
        return datetime.combine(awaiting.GO_LIVE + timedelta(days=days), datetime.min.time(), NY).replace(hour=11)

    def test_go_live_is_10_21_2026(self):
        self.assertEqual(awaiting.GO_LIVE, date(2026, 10, 21))

    def test_app_voucher_counts_qb_voucher_and_pre_go_live_voucher_do_not(self):
        a, la = self.order(self.table)
        b, lb = self.order(self.table)
        c, lc = self.order(self.table)
        self.receive(self.po(la), 1, self.after_go_live(), imported=True)              # QuickBooks voucher
        self.receive(self.po(lb), 1, self.after_go_live(-3))                           # app voucher before go-live
        v = self.receive(self.po(lc), 1, self.after_go_live(1))                        # app voucher after go-live
        rows = self.rows()
        self.assertNotIn(a.pk, rows)
        self.assertNotIn(b.pk, rows)
        self.assertEqual(rows[c.pk]['ready'], '1 of 1')
        self.assertEqual(rows[c.pk]['came_in'], timezone.localdate(v.posted_at))
        self.assertEqual(list(rows), [c.pk])

    def test_untick_hides_tick_shows(self):
        order, line = self.order(self.table)
        self.receive(self.po(line), 1, self.after_go_live())
        self.assertIn(order.pk, self.rows())
        Document.objects.filter(pk=order.pk).update(order_pickup=False)
        self.assertNotIn(order.pk, self.rows())
        page = self.client.get(URL).content.decode()
        self.assertNotIn(f'/pos/order/{order.token}/', page)
        Document.objects.filter(pk=order.pk).update(order_pickup=True)
        self.assertIn(f'/pos/order/{order.token}/', self.client.get(URL).content.decode())

    def test_stock_lines_do_not_count(self):
        Item.objects.filter(pk=self.table.pk).update(on_hand=5)
        order, line = self.order(self.table)
        self.assertNotIn(order.pk, self.rows())

    def test_part_received_in_qb_part_in_app(self):
        order, line = self.order(self.table, qty=3)
        po_line = self.po(line)
        self.receive(po_line, 1, self.after_go_live(), imported=True)
        self.assertNotIn(order.pk, self.rows())
        self.receive(po_line, 1, self.after_go_live(1))
        self.assertEqual(self.rows()[order.pk]['ready'], '1 of 3')

    def test_qb_special_piece_shares_qb_goods_with_the_older_order_app_goods_with_the_newer(self):
        chest = Item.objects.create(sku='30003', name='TEST CHEST SOLD', description='(SOLD TEST) CHEST', price='500.00',
                                    on_hand='0', tax_rate='7.000', department=self.special)
        older, lo = self.order(chest, imported=True, days_ago=400)
        newer, ln = self.order(chest, imported=True, days_ago=20)
        self.receive(self.po(lo, linked=False), 1, self.after_go_live(-200), imported=True)
        self.assertEqual(self.rows(), {})
        self.receive(self.po(ln, linked=False), 1, self.after_go_live(1))
        rows = self.rows()
        self.assertEqual(list(rows), [newer.pk])
        self.assertEqual(rows[newer.pk]['ready'], '1 of 1')

    def test_unticked_order_still_takes_its_share(self):
        """A delivery order is unticked but its piece is still its own: the next order doesn't inherit it."""
        chest = Item.objects.create(sku='30004', name='TEST CHEST SOLD 2', description='(SOLD TEST) CHEST', price='500.00',
                                    on_hand='0', tax_rate='7.000', department=self.special)
        delivery, ld = self.order(chest, imported=True, days_ago=40, pickup=False)
        pickup, lp = self.order(chest, imported=True, days_ago=10)
        self.receive(self.po(ld, linked=False), 1, self.after_go_live())
        self.assertEqual(self.rows(), {})

    def test_page_has_no_date_filter(self):
        page = self.client.get(URL).content.decode()
        self.assertNotIn('awaiting-since', page)
        self.assertNotIn('Came in within', page)
        self.assertIn('Orders with Hold for Pickup ticked show here', page)
