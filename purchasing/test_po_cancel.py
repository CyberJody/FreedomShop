# SPDX-License-Identifier: AGPL-3.0-or-later
"""cancelling a purchase order is clear and final."""
import uuid
from decimal import Decimal

from django.test import TestCase

from sales.models import Item
from sales.services import WorkflowError
from .catalog_quantities import catalog_quantities
from .models import Vendor, PurchaseDocument
from .services import save_purchase


def header(vendor, **kw):
    data = dict(token=uuid.uuid4(), version=0, vendor=vendor, status='open', notes='', terms='', ship_date=None,
                payment_due=None, invoice_number='')
    data.update(kw)
    return data


class R88CancelledPOTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='CHE', company='SAMPLE FURNITURE SUPPLY')
        self.item = Item.objects.create(sku='13539', name='CHE-LILPLAQUE-GSL', description='LITTLE WALL PLAQUE', price=34.95,
                                        on_hand=0, average_cost=17, order_cost=17, primary_vendor=self.vendor, tax_rate=7)
        self.po = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('6'), cost=Decimal('17'))], 'po', 'save')

    def cancel(self):
        response = self.client.post(f'/purchasing/po/{self.po.token}/', {'action': 'cancel', 'version': str(self.po.version)})
        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()

    def save_post(self, status='open'):
        return {'token': str(self.po.token), 'version': str(self.po.version), 'vendor': self.vendor.pk, 'status': status,
                'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '1', 'lines-0-line_id': self.po.lines.get().pk,
                'lines-0-item': self.item.pk, 'lines-0-quantity': '6', 'lines-0-cost': '17', 'action': 'save'}

    def test_confirm_names_the_po(self):
        page = self.client.get(f'/purchasing/po/{self.po.token}/').content.decode()
        self.assertIn(f'data-number="{self.po.number}"', page)
        self.assertIn("'? It will no longer be expected from the vendor.'", page)
        self.assertIn("'Cancel Purchase Order'", page)

    def test_cancelled_po_opens_read_only_with_print(self):
        self.cancel()
        self.assertEqual(self.po.status, 'cancelled')
        page = self.client.get(f'/purchasing/po/{self.po.token}/?saved=1').content.decode()
        self.assertIn(f'{self.po.number} cancelled.', page)
        self.assertIn('id="po-cancelled-note"', page)
        self.assertIn('<strong>Status: Cancelled</strong>', page)
        self.assertNotIn('id="purchase-form"', page)
        self.assertNotIn('value="cancel"', page)
        self.assertNotIn('Save Only', page)
        self.assertNotIn('Save &amp; Print', page)
        self.assertIn('0 (cancelled)', page)
        self.assertIn(f'/purchasing/po/{self.po.token}/pdf/', page)        # Print / PDF stay
        self.assertEqual(self.po.due, 0)

    def test_server_refuses_save_cancel_and_receiving(self):
        self.cancel()
        response = self.client.post(f'/purchasing/po/{self.po.token}/', self.save_post())
        self.assertEqual(response.status_code, 400)
        self.assertIn(f'{self.po.number} was cancelled. It can no longer be changed or received.', response.content.decode())
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'cancelled')                       # it used to re-open
        with self.assertRaisesMessage(WorkflowError, 'was cancelled'):
            save_purchase({'token': self.po.token, 'version': self.po.version}, [], 'po', 'cancel')
        with self.assertRaisesMessage(WorkflowError, f'{self.po.number} was cancelled. Nothing can be received on it.'):
            save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('17'),
                                                     source_line=self.po.lines.get().pk)], 'voucher', 'post', self.po)
        page = self.client.get(f'/purchasing/voucher/new/?po={self.po.token}')
        self.assertIn('was cancelled. Nothing can be received on it.', page.content.decode())
        self.assertEqual(page.context['formset'].initial, [])

    def test_cancelled_po_is_not_on_order(self):
        on_order = lambda: catalog_quantities(Item.objects.filter(pk=self.item.pk)).get().on_order
        self.assertEqual(on_order(), 6)
        self.cancel()
        self.assertEqual(on_order(), 0)
        list_page = self.client.get('/purchasing/orders/?status=cancelled').content.decode()
        self.assertIn(self.po.number, list_page)

    def test_cancelling_an_earlier_voucher_keeps_the_po_cancelled(self):
        line = self.po.lines.get()
        voucher = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('2'), cost=Decimal('17'), source_line=line.pk)],
                                'voucher', 'post', self.po)
        self.po.refresh_from_db()
        self.cancel()
        save_purchase({'token': voucher.token, 'version': voucher.version}, [], 'voucher', 'cancel', self.po)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'cancelled')
