# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a received voucher can't be put back on hold or re-saved; it can be reversed (Reverse Voucher,
which takes its items back out of stock and makes them due again on the PO). A cancelled voucher is final."""
from decimal import Decimal

from django.test import TestCase

from sales.models import Item
from sales.services import WorkflowError
from .models import Vendor, PurchaseDocument
from .services import save_purchase
from .test_po_cancel import header


class R95ReceivedVoucherTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='CHE', company='SAMPLE FURNITURE SUPPLY')
        self.item = Item.objects.create(sku='13539', name='CHE-LILPLAQUE-GSL', description='LITTLE WALL PLAQUE', price=34.95,
                                        on_hand=0, average_cost=17, order_cost=17, primary_vendor=self.vendor, tax_rate=7)
        self.po = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('6'), cost=Decimal('17'))], 'po', 'save')
        self.po_line = self.po.lines.get()
        self.voucher = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('4'), cost=Decimal('17'),
                                                                source_line=self.po_line.pk)], 'voucher', 'post', self.po)

    def state(self):
        self.item.refresh_from_db(); self.po_line.refresh_from_db(); self.voucher.refresh_from_db(); self.po.refresh_from_db()
        return (self.voucher.status, self.item.on_hand, self.po_line.received, self.po.status)

    def post(self, action):
        line = self.voucher.lines.get()
        return self.client.post(f'/purchasing/voucher/{self.voucher.token}/', {
            'token': str(self.voucher.token), 'version': str(self.voucher.version), 'vendor': self.vendor.pk,
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '1', 'lines-0-line_id': line.pk, 'lines-0-item': self.item.pk,
            'lines-0-quantity': '4', 'lines-0-cost': '17', 'lines-0-source_line': self.po_line.pk, 'action': action})

    def reverse(self):
        return self.client.post(f'/purchasing/voucher/{self.voucher.token}/', {'action': 'cancel', 'version': str(self.voucher.version)})

    def test_received_voucher_refuses_hold_and_resave(self):
        self.assertEqual(self.state(), ('posted', Decimal('4'), Decimal('4'), 'open'))
        for action in ('hold', 'post'):
            with self.subTest(action=action):
                response = self.post(action)
                self.assertEqual(response.status_code, 400)
                self.assertIn('was already received. To fix a receiving mistake, use Reverse Voucher', response.content.decode())
                # Put on Hold used to take the 4 back out of stock and re-open the PO line.
                self.assertEqual(self.state(), ('posted', Decimal('4'), Decimal('4'), 'open'))

    def test_received_voucher_is_read_only_with_reverse(self):
        page = self.client.get(f'/purchasing/voucher/{self.voucher.token}/').content.decode()
        self.assertIn('id="voucher-received-note"', page)
        self.assertIn('id="reverse-voucher"', page)
        self.assertIn('Reverse Receiving Voucher', page)
        for gone in ('id="purchase-form"', 'value="hold"', '>Save Only<', '>Cancel Voucher<'):
            with self.subTest(gone=gone):
                self.assertFalse(gone in page, gone)
        self.assertIn(f'/purchasing/voucher/{self.voucher.token}/pdf/', page)   # Print stays

    def test_reverse_restores_stock_and_po_then_is_final(self):
        response = self.reverse()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state(), ('cancelled', Decimal('0'), Decimal('0'), 'open'))
        self.assertEqual(self.po_line.due, Decimal('6'))
        page = self.client.get(f'/purchasing/voucher/{self.voucher.token}/?saved=1').content.decode()
        self.assertIn(f'{self.voucher.number} reversed.', page)
        self.assertIn('id="voucher-cancelled-note"', page)
        for gone in ('id="purchase-form"', 'id="reverse-voucher"', '>Save Only<', 'value="hold"'):
            with self.subTest(gone=gone):
                self.assertFalse(gone in page, gone)
        # A cancelled voucher used to be received all over again by Save Only.
        for action in ('post', 'hold'):
            with self.subTest(action=action):
                response = self.post(action)
                self.assertEqual(response.status_code, 400)
                self.assertIn('was cancelled. It can no longer be changed or received again.', response.content.decode())
        with self.assertRaisesMessage(WorkflowError, 'was cancelled'):
            save_purchase({'token': self.voucher.token, 'version': self.voucher.version}, [], 'voucher', 'cancel', self.po)
        self.assertEqual(self.state(), ('cancelled', Decimal('0'), Decimal('0'), 'open'))

    def test_held_voucher_still_saves_holds_and_cancels(self):
        held = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('2'), cost=Decimal('17'),
                                                        source_line=self.po_line.pk)], 'voucher', 'hold', self.po)
        held = save_purchase(dict(header(self.vendor), token=held.token, version=held.version),
                             [dict(line_id=held.lines.get().pk, item=self.item, quantity=Decimal('2'), cost=Decimal('17'),
                                   source_line=self.po_line.pk)], 'voucher', 'post', self.po)
        self.assertEqual(held.status, 'posted')
        self.assertEqual(self.state()[1:3], (Decimal('6'), Decimal('6')))
        self.assertEqual(PurchaseDocument.objects.get(pk=self.po.pk).status, 'closed')
