# SPDX-License-Identifier: AGPL-3.0-or-later
"""follow-up (matching QB): Customer Details shows Available Credit (limit - balance) and says 'No limit' for
Account Limit and Available Credit when the limit is 0 (0 = no limit), instead of '$0.00' and a blank."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from customers.models import Customer


class CustomerDetailsAvailableCreditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('r97admin', 'r97@example.com', 'x')
        self.client.force_login(self.user)

    def details(self, customer):
        return self.client.get(f'/customers/?customer={customer.pk}').content.decode()

    def test_limit_shows_available_amount(self):
        c = Customer.objects.create(first_name='Limit', last_name='Holder', charge_account=True, account_limit=Decimal('1000.00'))
        page = self.details(c)
        self.assertIn('<dt>Available Credit</dt><dd>$1,000.00</dd>', page)
        self.assertIn('<dt>Account Limit</dt><dd>$1,000.00</dd>', page)

    def test_no_limit_says_no_limit(self):
        c = Customer.objects.create(first_name='No', last_name='Limit', charge_account=True, account_limit=Decimal('0'))
        page = self.details(c)
        self.assertIn('<dt>Available Credit</dt><dd>No limit</dd>', page)
        self.assertIn('<dt>Account Limit</dt><dd>No limit</dd>', page)
