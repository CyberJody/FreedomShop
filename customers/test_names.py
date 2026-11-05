# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import Customer


class CustomerNameTests(TestCase):
    def setUp(self):
        self.source = {'table': 'Customer', 'columns': {'3': 'Dr.', '4': 'Alex & Sam', '5': 'de la Cruz'},
                       'raw_row_hex': 'source-evidence'}
        self.customer = Customer.objects.create(source_id='qbpos:sample', customer_number='000123',
            full_name='Dr. Alex & Sam de la Cruz', phone='000555', source_data=self.source,
            source_sha256='a' * 64)
        self.row = {'source_id': self.customer.source_id, 'customer_number': self.customer.customer_number,
                    'full_name': self.customer.full_name, 'source_data': self.source}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'customers.json'
        self.path.write_text(json.dumps([self.row]))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def restore(self, **options):
        output = StringIO()
        call_command('restore_customer_names', str(self.path), expected_sha256=self.digest, stdout=output, **options)
        self.customer.refresh_from_db()
        return output.getvalue()

    def test_restore_dry_run_commit_and_repeat_preserve_identity_and_history(self):
        from sales.models import Document
        doc = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='Historic Name')
        old_stamp = self.customer.imported_at
        self.assertIn('Would restore 1', self.restore())
        self.assertEqual(self.customer.first_name, '')
        self.assertIn('Restored 1', self.restore(commit=True))
        self.assertEqual((self.customer.title, self.customer.first_name, self.customer.last_name), ('Dr.', 'Alex & Sam', 'de la Cruz'))
        self.assertEqual(self.customer.full_name, self.row['full_name'])
        self.assertEqual(self.customer.version, 2)
        self.assertEqual(self.customer.source_data, self.source)
        self.assertEqual(self.customer.imported_at, old_stamp)
        self.assertEqual(self.customer.phone, '000555')
        self.assertIsNone(self.customer.locally_edited_at)
        self.assertIn('Restored 0', self.restore(commit=True))
        self.assertEqual(self.customer.version, 2)
        doc.refresh_from_db()
        self.assertEqual((doc.customer_id, doc.customer_name), (self.customer.pk, 'Historic Name'))

    def test_restore_rejects_changed_name_until_explicit_review(self):
        self.customer.full_name = 'Alex & Sammy de la Cruz'
        self.customer.locally_edited_at = timezone.now()
        self.customer.save()
        with self.assertRaises(CommandError):
            self.restore(commit=True)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.first_name, '')
        overrides = Path(self.directory.name) / 'overrides.json'
        overrides.write_text(json.dumps([dict(source_id=self.customer.source_id,
            expected_full_name=self.customer.full_name, first_name='Alex & Sammy', last_name='de la Cruz', title='')]))
        self.restore(commit=True, overrides=str(overrides))
        self.assertEqual(self.customer.first_name, 'Alex & Sammy')
        self.assertEqual(self.customer.source_data, self.source)

    def test_restore_rejects_wrong_hash_and_source_mismatch(self):
        with self.assertRaises(CommandError):
            call_command('restore_customer_names', str(self.path), expected_sha256='0'*64, commit=True)
        self.customer.source_data = {}
        self.customer.save()
        with self.assertRaises(CommandError):
            self.restore(commit=True)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.first_name, '')

    def test_import_maps_original_columns_and_protects_local_edits(self):
        call_command('import_customers', str(self.path), commit=True, stdout=StringIO())
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.first_name, self.customer.last_name), ('Alex & Sam', 'de la Cruz'))
        self.customer.first_name = 'Locally changed'
        self.customer.full_name = self.customer.composed_name
        self.customer.locally_edited_at = timezone.now()
        self.customer.save()
        call_command('import_customers', str(self.path), commit=True, stdout=StringIO())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.first_name, 'Locally changed')

    def test_form_saves_separate_names_and_copy_keeps_components(self):
        self.restore(commit=True)
        data = dict(first_name='Alex & Sammy', last_name='de la Cruz', title='Dr.', company='',
                    customer_number='000123', phone='000555', active=True, version=2)
        response = self.client.post(f'/customers/{self.customer.pk}/edit/', data)
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.full_name, 'Dr. Alex & Sammy de la Cruz')
        self.assertEqual(self.customer.last_name, 'de la Cruz')
        self.customer.charge_account = True
        self.customer.account_limit = 500
        self.customer.save()
        response = self.client.get('/customers/new/', {'copy': self.customer.pk})
        self.assertEqual(response.context['form']['first_name'].value(), 'Alex & Sammy')
        self.assertEqual(response.context['form']['last_name'].value(), 'de la Cruz')
        self.assertEqual(response.context['form']['customer_number'].value(), '')
        self.assertNotContains(response, 'name="full_name"')
        # copies use the new-customer Charge Account default, but never copy limits.
        self.assertTrue(response.context['form']['charge_account'].value())
        self.assertFalse(response.context['form']['account_limit'].value())
        response = self.client.get('/customers/', {'q': 'Sammy Cruz'})
        self.assertEqual([c.pk for c in response.context['page']], [self.customer.pk])
        self.assertEqual(self.client.post(f'/customers/{self.customer.pk}/edit/', data).status_code, 400)

    def test_company_only_and_unsplit_legacy_names(self):
        response = self.client.post('/customers/new/', dict(company='Example Company', active=True, version=1))
        self.assertEqual(response.status_code, 302)
        company = Customer.objects.get(company='Example Company')
        self.assertEqual((company.first_name, company.last_name, company.display_name), ('', '', 'Example Company'))
        response = self.client.get(f'/customers/{self.customer.pk}/edit/')
        self.assertContains(response, 'Existing name:')
        response = self.client.post(f'/customers/{self.customer.pk}/edit/', dict(company='Example', version=1))
        self.assertEqual(response.status_code, 400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.full_name, self.row['full_name'])
