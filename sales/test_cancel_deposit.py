# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from decimal import Decimal as D
from django.test import TestCase
from customers.models import Customer
from sales.models import Document, Line, OrderPayment
from sales.services import save_document, WorkflowError


class CancelDepositTests(TestCase):
    def setUp(self):
        self.customer=Customer.objects.create(source_id='cancel-test',full_name='TEST CANCEL',charge_account=True)
        self.order=Document.objects.create(kind='order',status='open',customer=self.customer,subtotal=100,tax=7,total=107)
        Line.objects.create(document=self.order,position=0,quantity=1,price=100,tax_rate=7)
        OrderPayment.objects.create(order=self.order,amount=50,method='cash')

    def cancel(self, **extra):
        return save_document(dict(action='cancel',version=self.order.version,**extra),'order',self.order.token)

    def test_without_confirmation_leaves_order_and_deposit_alone(self):
        with self.assertRaisesMessage(WorkflowError,'holds $50.00'):
            self.cancel()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status,'open')
        self.assertEqual(self.order.paid,D('50'))

    def test_cash_card_account_refund_and_cancel_atomic(self):
        for method in ('cash','credit','account'):
            with self.subTest(method=method):
                self.order.refresh_from_db()
                done=self.cancel(refund_deposit=True,deposit_refund_method=method,deposit_refund_amount='50')
                self.assertEqual(done.status,'cancelled')
                self.assertEqual(done.unused_payments,D('0'))
                self.assertEqual(done.payments.latest('pk').method,method)
                self.customer.refresh_from_db()
                self.assertEqual(self.customer.app_account_activity, D('-50') if method=='account' else D('0'))
                # Fresh order state for the next refund method.
                done.payments.filter(amount__lt=0).delete()
                done.status='open';done.save()

    def test_stale_amount_and_invalid_method_do_not_refund(self):
        for method, amount in [('cash','49'),('gift','50')]:
            with self.assertRaises(WorkflowError):
                self.cancel(refund_deposit=True,deposit_refund_method=method,deposit_refund_amount=amount)
        self.assertEqual(self.order.payments.count(),1)

    def test_refund_only_unapplied_balance_and_replay_rejected(self):
        Document.objects.create(kind='sale',status='completed',source_order=self.order,total=20,tax=0,subtotal=20)
        done=self.cancel(refund_deposit=True,deposit_refund_method='cash',deposit_refund_amount='30')
        self.assertEqual(done.payments.latest('pk').amount,D('-30'))
        with self.assertRaisesMessage(WorkflowError,'Someone changed'):
            self.cancel(refund_deposit=True,deposit_refund_method='cash',deposit_refund_amount='30')
        self.assertEqual(done.payments.count(),2)

    def test_account_requires_enabled_customer(self):
        self.customer.charge_account=False;self.customer.save()
        with self.assertRaisesMessage(WorkflowError,'Turn on Charge Account'):
            self.cancel(refund_deposit=True,deposit_refund_method='account',deposit_refund_amount='50')
        self.assertEqual(self.order.payments.count(),1)
