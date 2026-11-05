# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from threading import Barrier

from django.db import connection, close_old_connections
from django.test import TestCase, TransactionTestCase, Client, override_settings

from customers.models import Customer
from .models import Item, Document, StockMovement
from .services import save_document, WorkflowError


def item():
    return Item.objects.create(sku='TEST-CHAIR', description='Test chair', price='10.05', on_hand='3', tax_rate='7.000')


def payload(product, **kwargs):
    customer=Customer.objects.first() or Customer.objects.create(source_id='checkout',full_name='Checkout customer')
    data = dict(token=str(uuid.uuid4()), action='hold', version=0,customer_id=customer.pk,
                lines=[dict(item_id=product.pk, quantity='1', price='10.05')])
    data.update(kwargs)
    return data


class SalesTests(TestCase):
    def setUp(self):
        self.item = item()
        self.customer = Customer.objects.create(source_id='test', full_name='<Test Customer>', active=True)

    def test_hold_resume_complete_and_duplicate_submit(self):
        data = payload(self.item, quantity='ignored')
        doc = save_document(data, 'sale', data['token'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand, Decimal('3'))
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(doc.total, Decimal('10.75'))
        # The held document retains its original tax even if item tax changes.
        self.item.tax_rate = 9
        self.item.save()
        data['lines'][0]['id'] = doc.lines.get().pk
        data.update(version=doc.version, action='complete', payment_method='cash', tendered='20.00')
        completed = save_document(data, 'sale', data['token'])
        self.assertEqual(completed.total, Decimal('10.75'))
        self.assertEqual(completed.change, Decimal('9.25'))
        with self.assertRaisesMessage(WorkflowError, 'Someone changed'):
            save_document(data, 'sale', data['token'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand, Decimal('2'))
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(Document.objects.count(), 1)

    def test_insufficient_stock_can_be_sold_and_reversed(self):
        data = payload(self.item, action='complete', payment_method='cash', tendered='100')
        data['lines'] *= 4  # Aggregate repeated lines, not just each line's quantity.
        doc=save_document(data, 'sale', data['token'])
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,Decimal('-1'))
        self.assertEqual(StockMovement.objects.get().quantity,Decimal('-4'))
        save_document(dict(action='cancel',version=doc.version),'sale',doc.token)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,Decimal('3'))

    def test_orders_require_customer_and_preserve_special_items(self):
        data = dict(action='save', lines=[dict(description='Custom table', sku='TABLE', vendor='Test vendor', quantity='2', price='300.50')])
        with self.assertRaisesMessage(WorkflowError, 'customer is required'):
            save_document(data, 'order', uuid.uuid4())
        data['customer_id'] = self.customer.pk
        doc = save_document(data, 'order', uuid.uuid4())
        self.assertEqual(doc.status, 'open')
        self.assertEqual(doc.tax, Decimal('42.07'))
        self.assertEqual(doc.total, Decimal('643.07'))
        self.assertEqual(doc.lines.get().vendor, 'Test vendor')
        self.assertFalse(StockMovement.objects.exists())
        response = self.client.get(f'/pos/order/{doc.token}/')
        self.assertContains(response, 'Sales Order')
        self.assertNotContains(response, '<Test Customer>')
        self.assertContains(self.client.get('/pos/orders/'), '&lt;Test Customer&gt;')

    def test_history_filters_by_completion_not_hold_date(self):
        data = payload(self.item)
        doc = save_document(data, 'sale', data['token'])
        Document.objects.filter(pk=doc.pk).update(created_at=timezone.now()-timedelta(days=60))
        data.update(version=doc.version, action='complete', payment_method='cash', tendered='20')
        doc = save_document(data, 'sale', data['token'])
        self.assertContains(self.client.get('/pos/history/?period=7'), doc.number)

    def test_stale_edits_rejected_and_cancelled_documents_stay_cancelled(self):
        data = payload(self.item)
        doc = save_document(data, 'sale', data['token'])
        with self.assertRaisesMessage(WorkflowError, 'Someone changed'):
            save_document(data, 'sale', data['token'])
        data.update(version=doc.version, action='cancel')
        doc = save_document(data, 'sale', data['token'])
        self.assertEqual(doc.status, 'cancelled')
        # a cancelled receipt is finished; it no longer reopens by Put on Hold (or Save).
        data.update(version=doc.version, action='hold')
        with self.assertRaisesMessage(WorkflowError, 'This receipt was cancelled.'):
            save_document(data, 'sale', data['token'])
        self.assertEqual(Document.objects.get(pk=doc.pk).status, 'cancelled')

    def test_invalid_values_payment_and_unknown_stock_tax_rejected(self):
        for bad in ['NaN', 'Infinity', '-1', '0', '0.0001', '1e100', '99999999999999']:
            data = payload(self.item)
            data['lines'][0]['quantity'] = bad
            with self.subTest(bad=bad), self.assertRaises(WorkflowError):
                save_document(data, 'sale', data['token'])
        for method, amount in [('credit', '20'), ('cash', '1'), ('check', '20')]:
            data = payload(self.item, action='complete', payment_method=method, tendered=amount)
            with self.assertRaises(WorkflowError):
                save_document(data, 'sale', data['token'])
        self.item.on_hand = None
        self.item.save()
        data = payload(self.item, action='complete', payment_method='cash', tendered='20')
        with self.assertRaisesMessage(WorkflowError, 'Enter a stock quantity'):
            save_document(data, 'sale', data['token'])
        self.item.tax_rate = None
        self.item.save()
        self.item.on_hand=3;self.item.save()
        doc=save_document(data, 'sale', data['token'])
        self.assertEqual(doc.tax,Decimal('0.70'))

    def test_routes_lookup_filters_and_print(self):
        for route in ['/', '/pos/sale/new/', '/pos/order/new/', '/pos/held/', '/pos/orders/', '/pos/history/']:
            self.assertEqual(self.client.get(route).status_code, 200)
        for name in ['End of Day', 'Reward Manager', 'Clock In/Out', 'Financial Center']:
            self.assertNotContains(self.client.get('/'), name)
        self.assertEqual(self.client.get('/pos/lookup/customers/?q=Test').json()['results'][0]['id'], self.customer.pk)
        self.assertEqual(self.client.get('/pos/lookup/items/?q=chair').json()['results'][0]['id'], self.item.pk)
        data = payload(self.item)
        response = self.client.post('/pos/sale/new/', json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        url = response.json()['url']
        self.assertContains(self.client.get('/pos/held/'), Document.objects.get().number)
        self.assertContains(self.client.get('/pos/history/'), 'No saved sales receipts')
        self.assertEqual(self.client.get(url+'?print=1').status_code, 200)
        self.assertIn('no-store', self.client.get(url)['Cache-Control'])
        self.assertEqual(self.client.post('/pos/history/').status_code, 405)
        self.assertEqual(self.client.post('/pos/sale/new/', '[]', content_type='application/json').status_code, 400)

    @override_settings(MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'])
    def test_csrf(self):
        client = Client(enforce_csrf_checks=True)
        data = payload(self.item)
        self.assertEqual(client.post('/pos/sale/new/', json.dumps(data), content_type='application/json').status_code, 403)
        client.get('/pos/sale/new/')
        csrf = client.cookies['csrftoken'].value
        self.assertEqual(client.post('/pos/sale/new/', json.dumps(data), content_type='application/json', HTTP_X_CSRFTOKEN=csrf).status_code, 200)


class ConcurrentSalesTests(TransactionTestCase):
    def test_concurrent_checkouts_allow_negative_without_losing_stock_changes(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock concurrency requires PostgreSQL')
        product = item()
        product.on_hand = 1
        product.save()
        Customer.objects.create(source_id='concurrent',full_name='Concurrent test')
        barrier = Barrier(2)
        def checkout():
            close_old_connections()
            data = payload(product, action='complete', payment_method='cash', tendered='20')
            barrier.wait(timeout=10)
            try:
                save_document(data, 'sale', data['token'])
                return 'saved'
            except WorkflowError:
                return 'unavailable'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: checkout(), range(2)))
        self.assertCountEqual(results, ['saved', 'saved'])
        product.refresh_from_db()
        self.assertEqual(product.on_hand, -1)
        self.assertEqual(StockMovement.objects.count(), 2)
