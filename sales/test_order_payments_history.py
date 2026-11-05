# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): the app's sales order deposits and refunds are listed in Sales History beside receipts."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.utils import timezone

from customers.models import Customer
from .models import Document, OrderPayment


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class OrderPaymentsInSalesHistory(TestCase):
    def setUp(self):
        now = timezone.now()
        self.now = now
        self.pat = Customer.objects.create(source_id='C-PAT', full_name='PAT DEPOSITOR')
        self.other = Customer.objects.create(source_id='C-OTHER', full_name='OTHER BUYER')
        self.order = Document.objects.create(kind='order', status='open', customer=self.pat, customer_name='PAT DEPOSITOR',
                                             subtotal='200.00', tax='14.00', total='214.00', tax_location='Springfield')
        self.old_receipt = self.receipt(self.other, 'OTHER BUYER', now - timedelta(days=3), '50.00')
        self.deposit = OrderPayment.objects.create(order=self.order, amount='100.00', method='credit', card_type='Visa',
                                                   created_at=now - timedelta(days=2))
        self.new_receipt = self.receipt(self.other, 'OTHER BUYER', now - timedelta(days=1), '25.00')
        self.refund = OrderPayment.objects.create(order=self.order, amount='-20.00', method='cash', created_at=now - timedelta(hours=1))
        # The internal $0 tax adjustment row is never money taken: not listed.
        self.adjustment = OrderPayment.objects.create(order=self.order, amount='0', method='cash', adjustment=True, created_at=now)

    def receipt(self, customer, name, when, total):
        doc = Document.objects.create(kind='sale', status='completed', customer=customer, customer_name=name, subtotal=total,
                                      tax='0', total=total, payment_method='cash', tendered=total, was_completed=True, completed_at=when)
        return doc

    def page_rows(self, **params):
        return self.client.get('/pos/history/', params).context['page'].object_list

    def labels(self, **params):
        return [row.number_or_label for row in self.page_rows(**params)]

    def test_deposits_and_refunds_are_rows_sorted_with_receipts(self):
        so = self.order.numbered_label
        self.assertTrue(so.startswith('Sales Order '))
        self.assertEqual(self.labels(), [f'Refund · {so}', self.new_receipt.number_or_label, f'Deposit · {so}', self.old_receipt.number_or_label])
        self.assertEqual(self.labels(sort='completed_at'), list(reversed(self.labels())))
        page = self.client.get('/pos/history/')
        self.assertEqual(page.context['page'].paginator.count, 4)
        self.assertContains(page, f'<a href="/pos/order/{self.order.token}/">Deposit · {so}</a>', html=False)
        self.assertContains(page, '$100.00')
        self.assertContains(page, '<td>Visa</td>')
        self.assertContains(page, '$-20.00')

    def test_continuous_scroll_rows_open_the_order(self):
        data = self.client.get('/pos/history/', {'format': 'rows', 'offset': '0'}).json()
        self.assertEqual(data['total'], 4)
        deposit = [row for row in data['rows'] if row['name'].startswith('Deposit')][0]
        self.assertEqual(deposit['href'], f'/pos/order/{self.order.token}/')
        self.assertEqual(deposit['cells'][2:10], ['Completed', '', 'PAT DEPOSITOR', '', '', '$100.00', 'Visa', 'Springfield'])
        self.assertEqual(deposit['cells'][1], [{'text': f'Deposit · {self.order.numbered_label}', 'href': f'/pos/order/{self.order.token}/'}])

    def test_filters_apply_to_order_payments(self):
        so = self.order.numbered_label
        self.assertEqual(self.labels(customer=self.pat.pk), [f'Refund · {so}', f'Deposit · {so}'])
        self.assertEqual(self.labels(customer=self.other.pk), [self.new_receipt.number_or_label, self.old_receipt.number_or_label])
        self.assertEqual(self.labels(status='cancelled'), [])
        self.assertEqual(len(self.labels(status='all')), 4)
        self.assertEqual(self.labels(q='depositor'), [f'Refund · {so}', f'Deposit · {so}'])
        self.assertEqual(self.labels(q=self.order.number), [f'Refund · {so}', f'Deposit · {so}'])
        OrderPayment.objects.filter(pk=self.deposit.pk).update(created_at=self.now - timedelta(days=40))
        self.assertEqual(self.labels(period='30'), [f'Refund · {so}', self.new_receipt.number_or_label, self.old_receipt.number_or_label])

    def test_other_sorts_take_payment_columns(self):
        so = self.order.numbered_label
        self.assertEqual(self.labels(sort='-total'), [f'Deposit · {so}', self.old_receipt.number_or_label, self.new_receipt.number_or_label, f'Refund · {so}'])
        self.assertEqual([row.customer_name for row in self.page_rows(sort='customer_name')], ['OTHER BUYER', 'OTHER BUYER', 'PAT DEPOSITOR', 'PAT DEPOSITOR'])
        for sort in ('display_number', '-display_number', 'status', 'item_count', '-item_quantity', 'payment_method', 'tax_area', '-tax_area'):
            with self.subTest(sort=sort):
                self.assertEqual(len(self.labels(sort=sort)), 4)

    def test_paging_counts_both(self):
        for n in range(60):
            OrderPayment.objects.create(order=self.order, amount='1.00', method='cash', created_at=self.now - timedelta(days=5, minutes=n))
        page1 = self.client.get('/pos/history/').context['page']
        self.assertEqual((page1.paginator.count, len(page1.object_list)), (64, 50))
        page2 = self.client.get('/pos/history/', {'page': '2'}).context['page']
        self.assertEqual(len(page2.object_list), 14)
        self.assertEqual(self.client.get('/pos/history/', {'format': 'rows', 'offset': '50'}).json()['total'], 64)
