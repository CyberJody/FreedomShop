# SPDX-License-Identifier: AGPL-3.0-or-later
"""The certificate row, not an item/customer lock, prevents concurrent overspend."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from customers.models import Customer
from sales.models import GiftCertificate, GiftRedemption
from sales.services import save_document, WorkflowError


@skipUnless(connection.vendor == 'postgresql', 'Row locking requires PostgreSQL')
class GiftConcurrencyTests(TransactionTestCase):
    def test_two_customers_cannot_spend_one_balance_twice(self):
        customers = [Customer.objects.create(source_id=f'TEST-GC-{n}', full_name=f'TEST GC {n}') for n in range(3)]
        def save(customer, lines, payments):
            token=str(uuid.uuid4())
            return save_document(dict(action='complete', customer_id=customer.pk, lines=lines, payments=payments), 'sale', token)
        save(customers[0], [dict(item_name='GIFT CERTIFICATE', description='TEST certificate', certificate_number='TEST-RACE', quantity='1', price='150')], [dict(method='cash', amount='150')])
        barrier=Barrier(2)
        def spend(customer):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                save(customer, [dict(item_name='TEST MISC', description='TEST concurrent purchase', quantity='1', price='100', tax_rate='0')], [dict(method='gift', amount='100', reference='TEST-RACE')])
                return 'saved'
            except WorkflowError:
                return 'refused'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(spend, customers[1:]))
        self.assertEqual(sorted(result), ['refused','saved'])
        self.assertEqual(GiftCertificate.objects.get().balance, 50)
        self.assertEqual(GiftRedemption.objects.count(), 1)
