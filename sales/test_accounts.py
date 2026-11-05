# SPDX-License-Identifier: AGPL-3.0-or-later
"""Charge accounts on sales receipts (part 2), following a QuickBooks POS walkthrough."""
import uuid
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from customers.models import Customer

from . import accounts
from .models import Document, Item
from .printing import Receipt, render_receipt
from .services import WorkflowError, save_document


class ChargeAccountTests(TestCase):
    def setUp(self):
        self.item = Item.objects.create(sku='WC/CUSTOMITEMTEST', description='CUSTOM ITEM FOR TESTING', price='1500.00',
                                        on_hand='5', tax_rate='7.000')
        self.customer = Customer.objects.create(source_id='wc', full_name='DEMO HOME & HARDWARE', charge_account=True)

    def sale(self, lines, payments, customer=None, token=None, version=0, action='complete', **extra):
        token = token or str(uuid.uuid4())
        data = dict(token=token, version=version, action=action, customer_id=(customer or self.customer).pk, lines=lines,
                    payments=payments, **extra)
        return save_document(data, 'sale', token)

    def item_line(self, price='1500.00'):
        return dict(item_id=self.item.pk, quantity='1', price=price)

    def balance(self, customer=None):
        return Customer.objects.get(pk=(customer or self.customer).pk).account_balance

    def test_charge_then_pay_on_account_like_qb_video(self):
        # Receipt 1: $1,500 + $105 tax = $1,605; $500 credit card, $1,105 to the account.
        first = self.sale([self.item_line()], [dict(method='credit', amount='500.00'), dict(method='account', amount='1105.00')])
        self.assertEqual(first.total, Decimal('1605.00'))
        self.assertEqual(self.balance(), Decimal('1105.00'))
        self.assertEqual(accounts.balances(first), (Decimal('0.00'), Decimal('1105.00')))
        # Receipt 2: Payment On Account $1,105 paid by credit card; no tax, no stock.
        second = self.sale([dict(account_payment=True, price='1105.00', tax_rate='7', item_name='anything')],
                           [dict(method='credit', amount='1105.00')])
        line = second.lines.get()
        self.assertEqual((line.item_name, line.tax_rate, line.quantity, second.tax), (accounts.POA_NAME, Decimal('0'), Decimal('1'), Decimal('0')))
        self.assertEqual(self.balance(), Decimal('0.00'))
        self.assertEqual(accounts.balances(second), (Decimal('1105.00'), Decimal('0.00')))
        # Customer Detail: the payment is history, not a sale.
        from customers import detail
        stats = detail.statistics(self.customer)
        self.assertEqual((stats['sales'], stats['receipts']), (Decimal('1605.00'), 1))
        notes = [n for row in detail.history(self.customer) for n in row['notes']]
        self.assertEqual(sorted(notes), ['Charged to Account $1,105.00', 'Payment On Account $1,105.00'])
        # Cancelling the charge receipt takes the charge back off the account.
        save_document(dict(action='cancel', version=first.version), 'sale', first.token)
        self.assertEqual(self.balance(), Decimal('-1105.00'))

    def test_balance_builds_on_imported_qb_balance_and_prints(self):
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=Decimal('250.00'))
        doc = self.sale([self.item_line('100.00')], [dict(method='account', amount='107.00')])
        self.assertEqual(self.balance(), Decimal('357.00'))
        texts = []
        original_text, original_columns, original_wrap = Receipt.text, Receipt.columns, Receipt.wrap
        with mock.patch.object(Receipt, 'text', autospec=True, side_effect=lambda self, t, *a, **k: (texts.append(t), original_text(self, t, *a, **k))[1]), \
                mock.patch.object(Receipt, 'wrap', autospec=True, side_effect=lambda self, t, *a, **k: (texts.append(t), original_wrap(self, t, *a, **k))[1]), \
                mock.patch.object(Receipt, 'columns', autospec=True, side_effect=lambda self, cells, *a, **k: (texts.append(' '.join(c[0] for c in cells)), original_columns(self, cells, *a, **k))[1]):
            render_receipt(Document.objects.get(pk=doc.pk))
        joined = '\n'.join(texts)
        self.assertIn('Signature', joined)
        self.assertIn(accounts.SIGNATURE_TEXT, joined)
        self.assertIn('Previous Account Balance: $250.00', joined)
        self.assertIn('Account Balance: $357.00', joined)

    def test_rules(self):
        plain = Customer.objects.create(source_id='p', full_name='NOT CHARGE')
        with self.assertRaisesMessage(WorkflowError, 'not set up as a charge account'):
            self.sale([self.item_line('10.00')], [dict(method='account', amount='10.70')], customer=plain)
        with self.assertRaisesMessage(WorkflowError, 'cannot both charge'):
            self.sale([self.item_line('10.00'), dict(account_payment=True, price='5.00')], [dict(method='account', amount='15.70')])
        with self.assertRaisesMessage(WorkflowError, 'not Account'):
            Customer.objects.filter(pk=self.customer.pk).update(charge_account=False)
            self.sale([dict(account_payment=True, price='5.00')], [dict(method='account', amount='5.00')])
        Customer.objects.filter(pk=self.customer.pk).update(charge_account=True, account_limit=Decimal('100.00'))
        with self.assertRaisesMessage(WorkflowError, 'over the $100.00 account limit'):
            self.sale([self.item_line('100.00')], [dict(method='account', amount='107.00')])
        self.sale([self.item_line('100.00')], [dict(method='account', amount='107.00')], override_limit=True)
        self.assertEqual(self.balance(), Decimal('107.00'))
        with self.assertRaisesMessage(WorkflowError, 'reserved'):
            self.sale([dict(item_name='Payment On Account', description='x', quantity='1', price='5.00')], [dict(method='cash', amount='5.35')])
        with self.assertRaisesMessage(WorkflowError, 'only be taken on a sales receipt'):
            token = str(uuid.uuid4())
            save_document(dict(token=token, version=0, action='save', customer_id=self.customer.pk,
                               lines=[dict(account_payment=True, price='5.00')]), 'order', token)
        # Held receipts do not touch the balance until completed.
        self.sale([dict(account_payment=True, price='7.00')], [dict(method='cash', amount='7.00')], action='hold')
        self.assertEqual(self.balance(), Decimal('107.00'))

    def test_receipt_refuses_until_charge_account_endpoint_enables(self):
        plain = Customer.objects.create(source_id='endpoint', full_name='Plain')
        with self.assertRaisesMessage(WorkflowError, 'not set up as a charge account'):
            self.sale([self.item_line('10.00')], [dict(method='account', amount='10.70')], customer=plain)
        self.assertEqual(self.client.post(f'/customers/{plain.pk}/charge-account/').status_code, 200)
        receipt = self.sale([self.item_line('10.00')], [dict(method='account', amount='10.70')], customer=plain)
        self.assertEqual(receipt.status, 'completed')
        self.assertEqual(self.balance(plain), Decimal('10.70'))

    def test_register_page_has_account_dialog_and_customer_account_info(self):
        page = self.client.get('/pos/sale/new/', {'customer': self.customer.pk})
        self.assertContains(page, 'Take Payment for Charge Account')
        self.assertContains(page, 'id="account-dialog"')
        self.assertContains(page, '"charge_account": true')
        found = self.client.get('/pos/lookup/customers/', {'q': 'DEMO HOME & HARDWARE'}).json()['results'][0]
        self.assertEqual((found['charge_account'], found['account_balance']), (True, '0.00'))


class SalesOrderAccountTests(TestCase):
    """Sales orders: deposits paid by Account charge the account; payments on account open a receipt from the order."""

    def setUp(self):
        self.customer = Customer.objects.create(source_id='wc', full_name='DEMO HOME & HARDWARE', charge_account=True, account_limit=Decimal('1000.00'))
        token = str(uuid.uuid4())
        self.order = save_document(dict(token=token, version=0, action='save', customer_id=self.customer.pk,
                                        lines=[dict(item_name='CUSTOM TABLE', description='Custom table', quantity='1', price='1500.00')]), 'order', token)

    def pay(self, amount, method='account', **extra):
        from .services import save_order_payment
        self.order.refresh_from_db()
        return save_order_payment(self.order.pk, dict(version=self.order.version, token=str(uuid.uuid4()), amount=amount, method=method, **extra))

    def balance(self):
        return Customer.objects.get(pk=self.customer.pk).account_balance

    def test_deposit_by_account_charges_and_refund_credits(self):
        self.pay('400.00')
        self.assertEqual(self.balance(), Decimal('400.00'))
        self.order.refresh_from_db()
        self.assertEqual(self.order.paid, Decimal('400.00'))
        with self.assertRaisesMessage(WorkflowError, 'over the $1,000.00 account limit'):
            self.pay('700.00')
        self.pay('700.00', override_limit='1')
        self.assertEqual(self.balance(), Decimal('1100.00'))
        self.pay('-100.00')  # refund to Account
        self.assertEqual(self.balance(), Decimal('1000.00'))
        # Correcting a payment to another method and deleting one both re-figure the balance.
        from .services import save_order_payment
        first = self.order.payments.order_by('pk').first()
        self.order.refresh_from_db()
        save_order_payment(self.order.pk, dict(version=self.order.version, token=str(first.token), payment_id=first.pk, amount='400.00', method='cash'))
        self.assertEqual(self.balance(), Decimal('600.00'))
        last = self.order.payments.order_by('pk').last()
        self.order.refresh_from_db()
        save_order_payment(self.order.pk, dict(version=self.order.version, payment_id=last.pk, action='delete'))
        self.assertEqual(self.balance(), Decimal('700.00'))
        # Sales order printout shows the account lines.
        from .letter_printing import render_sales_order
        self.assertTrue(render_sales_order(Document.objects.get(pk=self.order.pk)))
        # A later payment-on-account receipt shows the order charge in its Previous Account Balance.
        token = str(uuid.uuid4())
        receipt = save_document(dict(token=token, version=0, action='complete', customer_id=self.customer.pk,
                                     lines=[dict(account_payment=True, price='700.00')], payments=[dict(method='check', amount='700.00')]), 'sale', token)
        self.assertEqual(accounts.balances(receipt), (Decimal('700.00'), Decimal('0.00')))
        self.assertEqual(self.balance(), Decimal('0.00'))

    def test_order_refuses_until_charge_account_endpoint_enables(self):
        Customer.objects.filter(pk=self.customer.pk).update(charge_account=False)
        with self.assertRaisesMessage(WorkflowError, 'not set up as a charge account'):
            self.pay('100.00')
        self.assertEqual(self.client.post(f'/customers/{self.customer.pk}/charge-account/').status_code, 200)
        self.pay('100.00')
        self.assertEqual(self.balance(), Decimal('100.00'))

    def test_non_charge_customer_and_order_page_links(self):
        Customer.objects.filter(pk=self.customer.pk).update(charge_account=False)
        with self.assertRaisesMessage(WorkflowError, 'not set up as a charge account'):
            self.pay('100.00')
        self.pay('100.00', method='credit')
        self.assertEqual(self.balance(), Decimal('0.00'))
        page = self.client.get(f'/pos/order/{self.order.token}/')
        link = f'/pos/sale/new/?customer={self.customer.pk}&amp;account_payment=1&amp;order={self.order.token}'
        self.assertContains(page, link)
        receipt_page = self.client.get(f'/pos/sale/new/?customer={self.customer.pk}&account_payment=1&order={self.order.token}')
        self.assertContains(receipt_page, '"open_account": "pay"')
        self.assertContains(receipt_page, f'Payment on account taken with sales order {self.order.number}')
