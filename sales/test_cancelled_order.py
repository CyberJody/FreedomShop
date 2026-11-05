# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a cancelled sales order is read-only, like a cancelled receipt or PO.

Status Cancelled with its date, nothing due (balance $0.00), payments/refunds history still shown, Print stays;
no Record Payment / Pickup / Cancel / Save. The server refuses payments, pickups, edits and a second Cancel.
"""
import uuid
from decimal import Decimal as D

from django.test import TestCase

from accounts.signin_helpers import sign_in_admin
from customers.models import Customer
from sales.models import Document, Line, OrderPayment
from sales.services import WorkflowError, save_document, save_order_payment, fulfill_order
from sales.test_receipt_lock import SESSIONS


class CancelledOrderTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='r95-test', full_name='TEST R95', charge_account=True)
        self.order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='TEST R95',
                                             subtotal=100, tax=7, total=107)
        self.line = Line.objects.create(document=self.order, position=0, quantity=1, price=100, tax_rate=7,
                                        item_name='TEST CHAIR', description='TEST CHAIR')
        OrderPayment.objects.create(order=self.order, amount=50, method='cash')
        self.order.refresh_from_db()
        self.order = save_document(dict(action='cancel', version=self.order.version, refund_deposit=True,
                                        deposit_refund_method='cash', deposit_refund_amount='50'), 'order', self.order.token)

    def test_cancelled_order_owes_nothing(self):
        self.assertEqual(self.order.status, 'cancelled')
        self.assertEqual(self.order.balance_due, D('0.00'))
        self.assertEqual(self.order.paid, D('0.00'))  # 50 deposit, 50 refunded: history kept

    def test_server_refuses_save_and_second_cancel(self):
        version = self.order.version
        with self.assertRaisesMessage(WorkflowError, 'was cancelled. It can no longer be changed.'):
            save_document(dict(action='save', version=version, customer_id=self.customer.pk,
                               lines=[dict(id=self.line.pk, quantity='1', price='100.00', tax_rate='7', item_name='TEST CHAIR')]),
                          'order', self.order.token)
        with self.assertRaisesMessage(WorkflowError, 'was cancelled. It can no longer be changed.'):
            save_document(dict(action='cancel', version=version), 'order', self.order.token)
        doc = Document.objects.get(pk=self.order.pk)
        self.assertEqual((doc.status, doc.version, doc.payments.count()), ('cancelled', version, 2))

    def test_server_refuses_payments_and_pickups(self):
        version = self.order.version
        with self.assertRaisesMessage(WorkflowError, 'Payments can no longer be recorded or changed on it.'):
            save_order_payment(self.order.pk, dict(version=version, token=str(uuid.uuid4()), amount='10', method='cash'))
        refund = self.order.payments.get(amount__lt=0)
        with self.assertRaisesMessage(WorkflowError, 'Payments can no longer be recorded or changed on it.'):
            save_order_payment(self.order.pk, dict(version=version, action='delete', payment_id=str(refund.pk)))
        with self.assertRaisesMessage(WorkflowError, 'Nothing can be picked up on it.'):
            fulfill_order(self.order.pk, {'version': str(version), f'qty_{self.line.pk}': '1'})
        self.assertEqual(Document.objects.get(pk=self.order.pk).payments.count(), 2)
        self.assertFalse(Document.objects.filter(source_order=self.order).exists())


@SESSIONS
class CancelledOrderScreenTests(TestCase):
    setUp_order = CancelledOrderTests.setUp

    def setUp(self):
        self.setUp_order()
        sign_in_admin(self.client)

    def test_open_order_still_has_its_actions(self):
        order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='TEST R95',
                                        subtotal=100, tax=7, total=107)
        Line.objects.create(document=order, position=0, quantity=1, price=100, tax_rate=7, item_name='TEST CHAIR')
        html = self.client.get(f'/pos/order/{order.token}/').content.decode()
        for kept in ('>Record Payment<', 'type="button" data-action="cancel"', '>Save Only<', 'Balance due $107.00'):
            with self.subTest(kept=kept):
                self.assertTrue(kept in html, kept)

    def test_screen_is_read_only_with_history_and_print(self):
        html = self.client.get(f'/pos/order/{self.order.token}/').content.decode()
        self.assertIn('id="order-cancelled-strip"', html)
        self.assertIn('Cancelled: nothing due', html)
        self.assertFalse('Balance due $107' in html)
        self.assertIn('Deposit refund on cancellation', html)   # payments/refunds history stays
        for gone in ('>Record Payment<', '>Complete Selected Pickup<', 'type="button" data-action="cancel"', '>Save Only<',
                     'type="button" data-special-open>', '>Take Payment for Charge Account</a>', '>Delete Payment<'):
            with self.subTest(gone=gone):
                self.assertFalse(gone in html, gone)
        self.assertIn('id="print-document"', html)

    def test_cancel_says_order_cancelled(self):
        order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='TEST R95',
                                        subtotal=100, tax=7, total=107)
        Line.objects.create(document=order, position=0, quantity=1, price=100, tax_rate=7, item_name='TEST CHAIR')
        response = self.client.post(f'/pos/order/{order.token}/', data=dict(action='cancel', version=order.version,
                                                                             token=str(order.token)),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        url = response.json()['url']
        self.assertTrue(url.endswith('?saved=order-cancelled'))
        self.assertContains(self.client.get(url), 'Order cancelled.')

    def test_order_list_shows_cancelled_with_no_balance(self):
        html = self.client.get('/pos/orders/?status=cancelled&period=all').content.decode()
        row = html[html.index(f'{self.order.token}'):]
        row = row[:row.index('</tr>')]
        self.assertIn('Cancelled', row)
        self.assertIn('<td>$0.00</td><td>—</td>', row)
