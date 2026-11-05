# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a PO made from customer orders only offers the lines whose item's vendor is the PO's vendor."""
import uuid
from decimal import Decimal

from django.test import TestCase

from customers.models import Customer
from sales.models import Item, Document, Line
from .models import Vendor, ItemVendor, PurchaseDocument
from .services import save_purchase
from sales.services import WorkflowError


class R87VendorLinesTests(TestCase):
    def setUp(self):
        self.bylers = Vendor.objects.create(code='BW/', company='DEMO WOODSHOP')
        self.miller = Vendor.objects.create(code='WMF', company='SAMPLE FORESTRY')
        self.apron = Item.objects.create(sku='16986', name='BW/RWAL-APRON/LEG-NAT  SOLD', description='(SOLD 5379 GROSSMAN) BASE',
                                         price=900, on_hand=0, average_cost=0, order_cost=300, primary_vendor=self.bylers, tax_rate=7)
        self.top = Item.objects.create(sku='16985', name='WMF4284RWAL  SOLD', description='(SOLD 5379 GROSSMAN) TOP',
                                       price=1800, on_hand=0, average_cost=0, order_cost=700, primary_vendor=self.miller, tax_rate=7)
        self.loose = Item.objects.create(sku='17001', name='NO VENDOR SOLD', description='(SOLD MASCHEWSKI) piece',
                                         price=100, on_hand=0, average_cost=0, order_cost=40, tax_rate=7)
        customer = Customer.objects.create(source_id='gross', full_name='ALEX EXAMPLE')
        self.order = Document.objects.create(kind='order', status='open', customer=customer, customer_name='ALEX EXAMPLE')
        self.top_line = Line.objects.create(document=self.order, position=0, item=self.top, description='top', quantity=1, price=1800, tax_rate=7)
        self.apron_line = Line.objects.create(document=self.order, position=1, item=self.apron, description='base', quantity=1, price=900, tax_rate=7)

    def page(self, extra=''):
        response = self.client.get(f'/purchasing/po/new/?order={self.order.token}{extra}')
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def test_two_vendors_ask_for_one_and_offer_nothing_yet(self):
        response, page = self.page()
        self.assertEqual(response.context['formset'].initial, [])
        self.assertIn('id="po-vendor-choices"', page)
        self.assertIn('Choose the vendor for this PO', page)
        self.assertIn(f'?order={self.order.token}&amp;vendor={self.bylers.pk}', page)
        self.assertIn('DEMO WOODSHOP (1 item)', page)
        self.assertIn('SAMPLE FORESTRY (1 item)', page)

    def test_choosing_bylers_offers_only_the_bylers_line(self):
        response, page = self.page(f'&vendor={self.bylers.pk}')
        self.assertEqual([r['sales_line'] for r in response.context['formset'].initial], [self.apron_line.pk])
        self.assertEqual(response.context['header'].initial['vendor'], self.bylers.pk)
        self.assertIn('These orders also have items from other vendors.', page)
        self.assertIn('id="po-vendor-follow"', page)

    def test_one_vendor_order_picks_it_without_asking(self):
        self.top_line.delete()
        response, page = self.page()
        self.assertEqual([r['sales_line'] for r in response.context['formset'].initial], [self.apron_line.pk])
        self.assertEqual(response.context['header'].initial['vendor'], self.bylers.pk)
        self.assertNotIn('id="po-vendor-choices"', page)

    def test_item_with_no_vendor_is_listed_not_offered(self):
        Line.objects.create(document=self.order, position=2, item=self.loose, description='piece', quantity=1, price=100, tax_rate=7)
        response, page = self.page(f'&vendor={self.bylers.pk}')
        self.assertEqual([r['sales_line'] for r in response.context['formset'].initial], [self.apron_line.pk])
        self.assertIn('NO VENDOR SOLD: no vendor on this item; set its vendor in the Item List', page)

    def test_after_the_bylers_po_the_order_offers_only_miller(self):
        save_purchase(dict(token=uuid.uuid4(), version=0, vendor=self.bylers, status='open', notes='', terms='', invoice_number=''),
                      [dict(item=self.apron, quantity=Decimal('1'), cost=Decimal('300'), sales_line=self.apron_line.pk)], 'po', 'save')
        response, page = self.page()
        self.assertEqual([r['sales_line'] for r in response.context['formset'].initial], [self.top_line.pk])
        self.assertEqual(response.context['header'].initial['vendor'], self.miller.pk)

    def test_server_refuses_another_vendors_line(self):
        with self.assertRaisesMessage(WorkflowError, 'WMF4284RWAL  SOLD is ordered from SAMPLE FORESTRY, not DEMO WOODSHOP'):
            save_purchase(dict(token=uuid.uuid4(), version=0, vendor=self.bylers, status='open', notes='', terms='', invoice_number=''),
                          [dict(item=self.top, quantity=Decimal('1'), cost=Decimal('700'), sales_line=self.top_line.pk)], 'po', 'save')
        self.assertFalse(PurchaseDocument.objects.exists())

    def test_an_alternate_vendor_may_take_the_line(self):
        ItemVendor.objects.create(item=self.top, vendor=self.bylers, order_cost=650)
        po = save_purchase(dict(token=uuid.uuid4(), version=0, vendor=self.bylers, status='open', notes='', terms='', invoice_number=''),
                           [dict(item=self.top, quantity=Decimal('1'), cost=Decimal('650'), sales_line=self.top_line.pk)], 'po', 'save')
        self.assertEqual(po.lines.get().sales_line, self.top_line)

    def test_selected_orders_from_the_order_list_filter_the_same_way(self):
        customer = Customer.objects.create(source_id='other', full_name='OTHER')
        other = Document.objects.create(kind='order', status='open', customer=customer, customer_name='OTHER')
        other_line = Line.objects.create(document=other, position=0, item=self.apron, description='x', quantity=2, price=900, tax_rate=7)
        response = self.client.get(f'/purchasing/po/new/?order={self.order.token}&order={other.token}&vendor={self.bylers.pk}')
        self.assertEqual(sorted(r['sales_line'] for r in response.context['formset'].initial), sorted([self.apron_line.pk, other_line.pk]))
