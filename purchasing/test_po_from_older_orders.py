# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): Generate PO / Create PO from Selected Orders no longer offers order lines that are already on a
PO when that PO isn't linked to the order (every PO brought over from QuickBooks). Matched by item."""
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from customers.models import Customer
from sales.models import Item, Document, Line
from .models import Vendor, Department, PurchaseDocument, PurchaseLine
from .order_coverage import coverage, matched_purchase_orders


def at(day):
    return datetime(2026, 9, day, 15, 0, tzinfo=dt_timezone.utc)


class OlderOrdersBase(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='BAM', company="DEMO WOODCRAFT")
        self.special_dept = Department.objects.create(code='SPE', name='SPECIAL ORDER')
        self.chest = Item.objects.create(sku='16851', name='BAM214BM-SEELY/EARTHTONE SOLD', description='(SOLD 5335 RICE) CUSTOM ASPEN 3-DRW CHEST',
                                         price=2000, on_hand=0, average_cost=0, order_cost=None, primary_vendor=self.vendor,
                                         department=self.special_dept, tax_rate=7)
        self.betty = Customer.objects.create(source_id='rice', full_name='BLAIR SAMPLE')

    def imported_order(self, item, quantity=1, day=9, customer=None, sku=None, name=None, description=None):
        customer = customer or self.betty
        order = Document.objects.create(kind='order', status='open', source_id=f'qb-so-{uuid.uuid4()}', source_number='5335',
                                        customer=customer, customer_name=customer.full_name)
        Document.objects.filter(pk=order.pk).update(created_at=at(day))
        order.refresh_from_db()
        line = Line.objects.create(document=order, position=0, item=item, sku=sku or (item.sku if item else ''),
                                   item_name=name or (item.name if item else ''), description=description or (item.description if item else ''),
                                   quantity=quantity, price=2000, tax_rate=7)
        return order, line

    def imported_po(self, item, quantity=1, received=0, day=9, status='open', sku=None, number=None):
        self.po_number = getattr(self, 'po_number', 8134) + 1
        po = PurchaseDocument.objects.create(kind='po', status=status, vendor=self.vendor, vendor_name=self.vendor.company,
                                             source_id=f'qb-po-{uuid.uuid4()}', source_number=number or str(self.po_number))
        PurchaseDocument.objects.filter(pk=po.pk).update(created_at=at(day))
        po.refresh_from_db()
        PurchaseLine.objects.create(document=po, source_id=f'{po.source_id}:1', position=0, item=item, sku=sku or (item.sku if item else ''),
                                    item_name=item.name if item else 'GONE SOLD', description='x', quantity=quantity, cost=1025,
                                    received=received, imported_received=received)
        return po

    def offered(self, *orders):
        query = '&'.join(f'order={o.token}' for o in orders)
        response = self.client.get(f'/purchasing/po/new/?{query}')
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        return page, [f for f in response.context['formset'].initial]


class OlderOrdersSpecialOrderTests(OlderOrdersBase):
    def test_betty_rice_chest_already_on_a_qb_po_is_not_offered(self):
        order, line = self.imported_order(self.chest)
        po = self.imported_po(self.chest)
        page, rows = self.offered(order)
        self.assertEqual(rows, [])
        self.assertIn('id="po-left-off"', page)
        self.assertIn(f'1 already on {po.number}', page)

    def test_received_counts_as_ordered_and_a_closed_po_counts(self):
        order, line = self.imported_order(self.chest)
        self.imported_po(self.chest, received=1, status='closed')
        self.assertEqual(coverage([line])[line.pk].remaining, 0)

    def test_cancelled_po_does_not_count(self):
        order, line = self.imported_order(self.chest)
        self.imported_po(self.chest, status='cancelled')
        page, rows = self.offered(order)
        self.assertEqual([r['sales_line'] for r in rows], [line.pk])

    def test_partly_covered_quantity_offers_only_the_remainder(self):
        order, line = self.imported_order(self.chest, quantity=4)
        po = self.imported_po(self.chest, quantity=1)
        page, rows = self.offered(order)
        self.assertEqual([(r['sales_line'], r['quantity']) for r in rows], [(line.pk, Decimal('3'))])
        self.assertIn(f'1 already on {po.number}; 3 offered below', page)

    def test_unordered_special_item_is_still_offered(self):
        order, line = self.imported_order(self.chest, quantity=2)
        page, rows = self.offered(order)
        self.assertEqual([(r['sales_line'], r['quantity']) for r in rows], [(line.pk, Decimal('2'))])
        self.assertNotIn('id="po-left-off"', page)

    def test_two_order_lines_share_one_po_quantity_oldest_first(self):
        first, old_line = self.imported_order(self.chest, day=1)
        second, new_line = self.imported_order(self.chest, day=5)
        self.imported_po(self.chest)
        result = coverage([new_line, old_line])
        self.assertEqual((result[old_line.pk].remaining, result[new_line.pk].remaining), (0, 1))

    def test_hand_typed_po_for_an_app_order_special_item_counts(self):
        order = Document.objects.create(kind='order', status='open', customer=self.betty, customer_name='BLAIR SAMPLE')
        line = Line.objects.create(document=order, position=0, item=self.chest, description='chest', quantity=1, price=2000, tax_rate=7)
        app_po = PurchaseDocument.objects.create(kind='po', status='open', vendor=self.vendor, vendor_name=self.vendor.company)
        PurchaseLine.objects.create(document=app_po, position=0, item=self.chest, sku=self.chest.sku, description='x', quantity=1, cost=1025)
        self.assertEqual(coverage([line])[line.pk].remaining, 0)

    def test_linked_po_lines_keep_the_old_rule(self):
        order = Document.objects.create(kind='order', status='open', customer=self.betty, customer_name='BLAIR SAMPLE')
        line = Line.objects.create(document=order, position=0, item=self.chest, description='chest', quantity=2, price=2000, tax_rate=7)
        app_po = PurchaseDocument.objects.create(kind='po', status='open', vendor=self.vendor, vendor_name=self.vendor.company)
        PurchaseLine.objects.create(document=app_po, position=0, item=self.chest, sku=self.chest.sku, description='x', quantity=1, cost=1025, sales_line=line)
        self.imported_po(self.chest, quantity=5)   # unlinked quantity is not used for a line that has a linked PO line
        self.assertEqual(coverage([line])[line.pk].remaining, 1)

    def test_item_on_two_customers_orders_is_not_a_special_piece(self):
        other = Customer.objects.create(source_id='other', full_name='OTHER CUSTOMER')
        order, line = self.imported_order(self.chest)
        self.imported_order(self.chest, customer=other)
        self.imported_po(self.chest)    # dated the same day as the order: the stock rule (PO on/after the order date) applies
        result = coverage([line])[line.pk]
        self.assertEqual(result.remaining, 0)
        self.assertIn('on or after this order', result.reason)

    def test_qb_deleted_item_is_matched_by_its_item_number(self):
        order, line = self.imported_order(None, sku='9235', name='PMW 1800-50-BK SINGLE SOLD', description='(SOLD 2061 ZARGHAM) ECLIPSE')
        self.imported_po(None, sku='9235')
        self.assertEqual(coverage([line])[line.pk].remaining, 0)
        page, rows = self.offered(order)
        self.assertEqual(rows, [])

    def test_view_pos_lists_the_matched_qb_po(self):
        order, line = self.imported_order(self.chest)
        po = self.imported_po(self.chest)
        unrelated = self.imported_po(Item.objects.create(sku='X1', name='OTHER SOLD', description='(SOLD X) other', price=1, primary_vendor=self.vendor))
        self.assertEqual(list(matched_purchase_orders(order)), [po])
        page = self.client.get(f'/purchasing/orders/?order={order.token}&status=all').content.decode()
        self.assertIn(po.number, page)
        self.assertNotIn(unrelated.number, page[page.index('<tbody>'):])
        # The PO List and the QB PO itself name the order the piece is for.
        self.assertIn(f'{order.number} · BLAIR SAMPLE', self.client.get('/purchasing/orders/?status=all').content.decode())
        self.assertIn(f'{order.numbered_label} · BLAIR SAMPLE', self.client.get(f'/purchasing/po/{po.token}/').content.decode())


class OlderOrdersStockItemTests(OlderOrdersBase):
    def setUp(self):
        super().setUp()
        self.chair = Item.objects.create(sku='641', name='HOS-CFA-45-WW/BRN', description='COMFY BACK FOLDING ADK CHAIR',
                                         price=480, on_hand=0, average_cost=211, order_cost=211, primary_vendor=self.vendor, tax_rate=7)

    def test_imported_order_stock_line_with_a_later_po_is_left_off_with_a_note(self):
        order, line = self.imported_order(self.chair, quantity=2, day=9)
        po = self.imported_po(self.chair, quantity=12, day=10)
        page, rows = self.offered(order)
        self.assertEqual(rows, [])
        self.assertIn(f'on {po.number} dated 9/10/2026, on or after this order', page)

    def test_imported_order_stock_line_with_only_an_older_po_is_offered(self):
        order, line = self.imported_order(self.chair, quantity=2, day=9)
        self.imported_po(self.chair, quantity=12, day=1)
        page, rows = self.offered(order)
        self.assertEqual([(r['sales_line'], r['quantity']) for r in rows], [(line.pk, Decimal('2'))])

    def test_imported_order_stock_line_in_stock_is_left_off(self):
        Item.objects.filter(pk=self.chair.pk).update(on_hand=5)
        order, line = self.imported_order(self.chair, quantity=2)
        result = coverage([Line.objects.select_related('item', 'document').get(pk=line.pk)])[line.pk]
        self.assertEqual((result.remaining, result.reason), (0, 'in stock (5 on hand)'))

    def test_app_order_stock_line_keeps_the_old_rule(self):
        Item.objects.filter(pk=self.chair.pk).update(on_hand=5)
        order = Document.objects.create(kind='order', status='open', customer=self.betty, customer_name='BLAIR SAMPLE')
        line = Line.objects.create(document=order, position=0, item=self.chair, description='chair', quantity=2, price=480, tax_rate=7)
        self.imported_po(self.chair, quantity=12, day=28)
        self.assertEqual(coverage([Line.objects.select_related('item', 'document').get(pk=line.pk)])[line.pk].remaining, 2)
