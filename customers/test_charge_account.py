# SPDX-License-Identifier: AGPL-3.0-or-later
from django.test import Client, TestCase, override_settings
from .forms import CustomerForm
from .models import Customer


class ChargeAccountDefaultTests(TestCase):
    def test_new_customer_default_and_model_unchanged(self):
        self.assertTrue(CustomerForm()['charge_account'].value())
        self.assertFalse(Customer().charge_account)
        response = self.client.get('/customers/new/')
        self.assertTrue(response.context['form']['charge_account'].value())

    def test_copy_defaults_charge_on_limit_blank_quickbooks_unchanged(self):
        source = Customer.objects.create(source_id='copy', full_name='Copy', charge_account=False,
                                         account_limit='123.45', use_with_quickbooks=True)
        response = self.client.get('/customers/new/', {'copy': source.pk})
        form = response.context['form']
        self.assertTrue(form['charge_account'].value())
        self.assertIsNone(form['account_limit'].value())
        self.assertEqual(form['use_with_quickbooks'].value(), Customer().use_with_quickbooks)
        source.refresh_from_db()
        self.assertFalse(source.charge_account)

    def test_edit_keeps_existing_false_and_limit(self):
        customer = Customer.objects.create(source_id='edit', full_name='Edit', charge_account=False, account_limit='42.00')
        form = self.client.get(f'/customers/{customer.pk}/edit/').context['form']
        self.assertFalse(form['charge_account'].value())
        self.assertEqual(str(form['account_limit'].value()), '42.00')

    def test_enable_post_only_unknown_404_and_no_other_changes(self):
        customer = Customer.objects.create(source_id='enable', full_name='Enable', account_limit='90.00',
                                           imported_account_balance='12.34', app_account_activity='5.00')
        before = Customer.objects.filter(pk=customer.pk).values().get()
        url = f'/customers/{customer.pk}/charge-account/'
        for method in ('get', 'head', 'put', 'delete'):
            self.assertEqual(getattr(self.client, method)(url).status_code, 405)
        self.assertEqual(self.client.post('/customers/999999/charge-account/').status_code, 404)
        for _ in range(2):
            response = self.client.post(url, {'account_limit': '1.00', 'full_name': 'Ignored'})
            self.assertEqual(response.json(), {'ok': True, 'charge_account': True,
                                               'account_balance': '17.34', 'account_limit': '90.00'})
        after = Customer.objects.filter(pk=customer.pk).values().get()
        self.assertEqual(after, {**before, 'charge_account': True})

    @override_settings(MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'])
    def test_enable_requires_csrf(self):
        customer = Customer.objects.create(source_id='csrf', full_name='CSRF')
        self.assertEqual(Client(enforce_csrf_checks=True).post(f'/customers/{customer.pk}/charge-account/').status_code, 403)
        customer.refresh_from_db()
        self.assertFalse(customer.charge_account)


class TakePaymentButtonsTests(TestCase):
    """Take Payment on Account entry points open the receipt with the Charge Account dialog in pay mode."""
    def test_customer_list_sidebar_and_detail_link_to_the_payment_receipt(self):
        c = Customer.objects.create(full_name='PAY TEST', last_name='TEST', first_name='PAY', charge_account=True, imported_account_balance=100)
        page = self.client.get(f'/customers/?customer={c.pk}').content.decode()
        self.assertEqual(page.count(f'href="/pos/sale/new/?customer={c.pk}&amp;account_payment=1"'), 2)  # sidebar + Customer Detail
        self.assertIn('Take Payment on Account', page)
        self.assertNotIn('account_payment=1', self.client.get('/customers/').content.decode())  # nothing selected
        receipt = self.client.get(f'/pos/sale/new/?customer={c.pk}&account_payment=1')
        self.assertEqual(receipt.status_code, 200)
        self.assertIn('"open_account": "pay"', receipt.content.decode())
