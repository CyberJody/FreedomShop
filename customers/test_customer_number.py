# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): customers created in the app get the next customer number; the number never changes."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from .models import Customer


def make(source_id, number='', **extra):
    return Customer.objects.create(source_id=source_id, customer_number=number, full_name=extra.pop('full_name', source_id),
                                   source_sha256='a' * 64, **extra)


class CustomerNumberTests(TestCase):
    def setUp(self):
        # QB's series: EAN-13 '0400001' + sequence + check digit; the rest are hand-typed QB oddities seen in the data.
        make('qbpos:0', '0400001713105')
        make('qbpos:1', '1400001509227')       # 13 digits, bad check digit
        make('qbpos:2', '04000015455845')      # 14 digits (numerically the largest)
        make('qbpos:6', '0410001504901')       # 13 digits above the series, bad check digit
        make('qbpos:3', 'Q')
        make('qbpos:4', '')

    def post_new(self, **changes):
        from .forms import CustomerForm
        values = {field: '' for field in CustomerForm.Meta.fields}
        values.update(first_name='New', last_name='Customer', version=1, tax_location='Springfield', account_limit='')
        values.update(changes)
        for flag in ('track_as_company', 'tax_exempt', 'email_opt_in', 'do_not_mail', 'use_with_quickbooks', 'charge_account'):
            values.pop(flag)
        values.update(accept_checks='on', active='on')
        return self.client.post('/customers/new/', values)

    def test_add_customer_gets_next_number_and_typed_number_is_ignored(self):
        response = self.post_new(customer_number='777')
        self.assertEqual(response.status_code, 302)
        first = Customer.objects.get(first_name='New')
        self.assertEqual(first.customer_number, '0400001713112')
        response = self.post_new(last_name='Second')
        second = Customer.objects.get(last_name='Second')
        self.assertEqual(second.customer_number, '0400001713129')
        page = self.client.get('/customers/', {'customer': second.pk})
        self.assertContains(page, '<dt>Customer #</dt><dd>0400001713129</dd>', html=False)

    def test_edit_never_changes_the_number(self):
        self.post_new()
        customer = Customer.objects.get(first_name='New')
        form = self.client.get(f'/customers/{customer.pk}/edit/').context['form']
        self.assertTrue(form.fields['customer_number'].disabled)
        values = {k: v for k, v in form.initial.items() if v is not None and not isinstance(v, bool)}
        values.update(customer_number='1', version=customer.version, accept_checks='on', active='on', account_limit='')
        response = self.client.post(f'/customers/{customer.pk}/edit/', values)
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertEqual(customer.customer_number, '0400001713112')
        self.assertEqual(customer.version, 2)

    def test_new_form_shows_placeholder(self):
        page = self.client.get('/customers/new/')
        self.assertContains(page, 'placeholder="Assigned when saved"')

    def test_imported_customers_are_never_numbered(self):
        customer = make('qbpos:5', '')
        self.assertEqual(customer.customer_number, '')

    def test_zero_padded_width_kept_and_empty_table_starts_at_one(self):
        """Fallback when no EAN-13 number is on file: highest all-digit number + 1."""
        Customer.objects.all().delete()
        self.assertEqual(make('local:a').customer_number, '1')
        Customer.objects.all().delete()
        make('qbpos:x', '000123')
        self.assertEqual(make('local:b').customer_number, '000124')

    def test_ean13_check_digit_matches_qb_numbers(self):
        for number in ('0400001713105', '0400001713099', '0400001713082', '0400001000045'):
            self.assertTrue(Customer.is_ean13(number), number)
        for number in ('1400001509227', '0410001504901', '04000015455845', 'Q', ''):
            self.assertFalse(Customer.is_ean13(number), number)

    def test_backfill_dry_run_then_commit_in_creation_order(self):
        older = make('local:older')
        newer = make('local:newer')
        Customer.objects.filter(pk__in=[older.pk, newer.pk]).update(customer_number='')
        out = StringIO()
        call_command('assign_customer_numbers', stdout=out)
        self.assertIn('app-made customers numbered: 2', out.getvalue())
        self.assertIn('imported customers with no number (left as they are): 1', out.getvalue())
        self.assertEqual(Customer.objects.get(pk=older.pk).customer_number, '')
        call_command('assign_customer_numbers', commit=True, stdout=StringIO())
        self.assertEqual(Customer.objects.get(pk=older.pk).customer_number, '0400001713112')
        self.assertEqual(Customer.objects.get(pk=newer.pk).customer_number, '0400001713129')
        self.assertEqual(Customer.objects.get(source_id='qbpos:4').customer_number, '')
        out = StringIO()
        call_command('assign_customer_numbers', commit=True, stdout=out)
        self.assertIn('app-made customers numbered: 0', out.getvalue())
