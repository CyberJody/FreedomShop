# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import tempfile
from io import StringIO
from pathlib import Path
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from .models import Customer


class CustomerTests(TestCase):
    def test_scroll_name_index_matches_filtered_order_before_rows_load(self):
        Customer.objects.create(source_id='a', full_name='Alpha', first_name=' Jane ', active=True)
        Customer.objects.create(source_id='b', full_name='Bravo', first_name='Émile', active=False)
        Customer.objects.create(source_id='c', full_name='Charlie', first_name='', company='Other', active=True)
        response = self.client.get('/customers/?status=all')
        self.assertEqual(response.context['scroll_config']['names'], ['Alpha', 'Bravo', 'Charlie'])
        response = self.client.get('/customers/?sort=-full_name&status=active')
        self.assertEqual(response.context['scroll_config']['names'], ['Charlie', 'Alpha'])
        response = self.client.get('/customers/?q=Jane')
        self.assertEqual(response.context['scroll_config']['names'], [])
        response = self.client.get('/customers/?q=Alpha')
        self.assertEqual(response.context['scroll_config']['names'], ['Alpha'])
        self.assertEqual(self.client.get('/customers/?view=pages').context['scroll_config']['names'], [])

    def test_scroll_batches_are_bounded_stable_filtered_and_read_only(self):
        Customer.objects.bulk_create([
            Customer(source_id=f'scroll:{i:04}', full_name='Same Name',
                     company='Scroll Company', city='Test City', active=i < 205)
            for i in range(212)
        ])
        params = {'format': 'rows', 'status': 'active', 'q': 'same test', 'sort': 'full_name'}
        ids = []
        for offset, size in [(0, 100), (100, 100), (200, 5), (300, 0)]:
            response = self.client.get('/customers/', {**params, 'offset': offset})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['total'], 205)
            self.assertEqual(data['offset'], offset)
            self.assertEqual(len(data['rows']), size)
            self.assertIn('no-store', response.headers['Cache-Control'])
            ids.extend(row['id'] for row in data['rows'])
            for row in data['rows']:
                self.assertNotIn('source_data', row)
                self.assertNotIn('source_sha256', row)
                self.assertNotIn('source_id', row)
        self.assertEqual(ids, list(Customer.objects.filter(active=True).order_by('full_name', 'source_id').values_list('pk', flat=True)))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.client.get('/customers/', {**params, 'status':'inactive'}).json()['total'], 7)
        self.assertEqual(self.client.get('/customers/', {**params, 'q':'no matching records'}).json()['rows'], [])
        for offset in ['-1', 'abc', '1.5', '9' * 100, '²']:
            self.assertEqual(self.client.get('/customers/', {**params, 'offset':offset}).status_code, 400)
        self.assertEqual(self.client.post('/customers/?format=rows').status_code, 405)
        self.assertEqual(Customer.objects.count(), 212)

    def test_scroll_names_and_sorting_and_paged_fallback(self):
        a = Customer.objects.create(source_id='a', full_name='<img src=x onerror=alert(1)>')
        z = Customer.objects.create(source_id='z', full_name='Zed')
        fallback = Customer.objects.create(source_id='fallback', company='Company only')
        data = self.client.get('/customers/?format=rows&sort=-full_name').json()
        self.assertEqual([row['id'] for row in data['rows']], [z.pk, a.pk, fallback.pk])
        self.assertEqual(data['rows'][1]['display_name'], a.full_name)
        self.assertEqual(data['rows'][2]['display_name'], 'Company only')
        response = self.client.get('/customers/?view=pages')
        self.assertContains(response, '&lt;img')
        self.assertContains(response, 'customer-pages')
        self.assertContains(response, 'customer-scroll-config')

    def test_search_matches_separated_words_and_requires_every_word(self):
        wanted = Customer.objects.create(source_id='shared-name', full_name='Pat & Taylor Example', city='Glens Falls')
        inactive = Customer.objects.create(source_id='inactive-name', full_name='Pat Example', active=False)
        Customer.objects.create(source_id='other-last-name', full_name='Pat Smith')
        Customer.objects.create(source_id='other-first-name', full_name='Taylor Example')
        for query in ('Pat Example', 'example pat', '  PAT   Example  ', 'Pat & Taylor Example', 'Pat Glens Falls'):
            with self.subTest(query=query):
                response = self.client.get('/customers/', {'q': query, 'status': 'active', 'sort': '-full_name'})
                self.assertEqual([customer.pk for customer in response.context['page']], [wanted.pk])
        response = self.client.get('/customers/', {'q': 'Pat Example', 'status': 'inactive'})
        self.assertEqual([customer.pk for customer in response.context['page']], [inactive.pk])
        response = self.client.get('/customers/', {'q': 'Pat Missing Example'})
        self.assertEqual(response.context['page'].paginator.count, 0)

    def test_navigation_search_details_and_pagination(self):
        Customer.objects.bulk_create([Customer(source_id=str(i), full_name=f'Customer {i:03}', source_sha256='a'*64) for i in range(52)])
        c = Customer.objects.create(source_id='special', full_name='<script>alert(1)</script>', company='Example Company', phone='555-1234', source_sha256='b'*64)
        self.assertContains(self.client.get('/'), 'href="/customers/"')
        response = self.client.get('/customers/')
        self.assertEqual(len(response.context['page']), 50)
        self.assertContains(response, 'Next')
        self.assertContains(self.client.get('/customers/?view=pages'), 'view=pages&amp;page=2')
        response = self.client.get('/customers/', {'q': 'Example', 'customer': c.pk})
        self.assertEqual(response.context['page'].paginator.count, 1)
        self.assertContains(response, '555-1234')
        self.assertContains(response, '&lt;script&gt;')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertContains(self.client.get('/customers/', {'q': 'absent'}), 'No customers match')
        self.assertEqual(self.client.post('/customers/').status_code, 405)
        self.assertEqual(self.client.get('/customers/', {'customer': 99999}).status_code, 404)
        self.assertEqual(self.client.get('/customers/', {'sort': 'invalid', 'page': 'bad'}).status_code, 200)

    def test_import_dry_run_idempotence_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'customers.json'
            rows = [{'source_id': '42', 'full_name': 'Example', 'postal_code': '01234', 'phone': '000123'}]
            path.write_text(json.dumps(rows))
            call_command('import_customers', str(path), stdout=StringIO())
            self.assertEqual(Customer.objects.count(), 0)
            call_command('import_customers', str(path), commit=True, stdout=StringIO())
            call_command('import_customers', str(path), commit=True, stdout=StringIO())
            self.assertEqual(Customer.objects.count(), 1)
            self.assertEqual(Customer.objects.get().postal_code, '01234')
            path.write_text(json.dumps(rows + rows))
            with self.assertRaises(CommandError):
                call_command('import_customers', str(path), commit=True, stdout=StringIO())
            self.assertEqual(Customer.objects.count(), 1)


class CustomerActionsTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='qbpos:original', full_name='Original Name', first_name='Original', last_name='Name',
            customer_number='0042', postal_code='01234', source_sha256='a'*64,
            source_data={'phone3': '000123', 'raw': 'evidence'})

    def fields(self, **changes):
        from .forms import CustomerForm
        values = {field: (getattr(self.customer, field) if getattr(self.customer, field) is not None else '') for field in CustomerForm.Meta.fields}
        values.update(changes)
        return values

    def test_edit_return_preserves_list_context_and_rejects_external_redirects(self):
        from urllib.parse import parse_qs, urlsplit
        return_to = '/customers/?q=Name&status=active&sort=city&at=413&page=9&customer=1'
        response = self.client.get(f'/customers/{self.customer.pk}/edit/', {'return_to':return_to})
        self.assertEqual(response.context['return_to'], return_to)
        response = self.client.post(f'/customers/{self.customer.pk}/edit/', self.fields(return_to=return_to))
        params = parse_qs(urlsplit(response.url).query)
        self.assertEqual(params['at'], ['413'])
        self.assertEqual(params['q'], ['Name'])
        self.assertEqual(params['sort'], ['city'])
        self.assertEqual(params['customer'], [str(self.customer.pk)])
        self.customer.refresh_from_db()
        for target in ['https://example.com/customers/', '//example.com/customers/', '/other/', 'https://[broken']:
            response = self.client.get(f'/customers/{self.customer.pk}/edit/', {'return_to':target})
            self.assertEqual(response.context['return_to'], '')

    def test_create_copy_edit_and_stale_submission_preserve_evidence(self):
        response = self.client.get('/customers/new/', {'copy': self.customer.pk})
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(response.context['form']['first_name'].value(), 'Original')
        self.assertEqual(response.context['form']['customer_number'].value(), '')
        response = self.client.post('/customers/new/', self.fields(first_name='New', last_name='Customer', customer_number=''))
        self.assertEqual(response.status_code, 302)
        created = Customer.objects.exclude(pk=self.customer.pk).get()
        self.assertTrue(created.source_id.startswith('local:'))
        self.assertEqual(created.source_data, {})
        self.assertIsNotNone(created.locally_edited_at)
        stamp = self.customer.imported_at
        response = self.client.post(f'/customers/{self.customer.pk}/edit/', self.fields(first_name='Edited', last_name='Name', active=False))
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.full_name, 'Edited Name')
        self.assertFalse(self.customer.active)
        self.assertEqual(self.customer.version, 2)
        self.assertEqual(self.customer.source_sha256, 'a'*64)
        self.assertEqual(self.customer.source_data['phone3'], '000123')
        self.assertEqual(self.customer.imported_at, stamp)
        response = self.client.post(f'/customers/{self.customer.pk}/edit/', self.fields(first_name='Stale', last_name='Name', version=1))
        self.assertContains(response, 'Someone changed this customer', status_code=400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.full_name, 'Edited Name')
        self.assertEqual(self.client.post('/customers/new/', self.fields(first_name='', last_name='', company='')).status_code, 400)
        self.assertEqual(self.client.get('/customers/new/?copy=invalid').status_code, 404)

    def test_import_preserves_local_edits_and_invalidates_old_forms(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'customers.json'
            path.write_text(json.dumps([{'source_id': self.customer.source_id, 'first_name': 'Imported', 'last_name': 'Name'}]))
            call_command('import_customers', str(path), commit=True, stdout=StringIO())
            self.customer.refresh_from_db()
            self.assertEqual(self.customer.version, 2)
            self.assertEqual(self.customer.full_name, 'Imported Name')
            self.client.post(f'/customers/{self.customer.pk}/edit/', self.fields(first_name='Local', last_name='Name'))
            output = StringIO()
            call_command('import_customers', str(path), commit=True, stdout=output)
            self.customer.refresh_from_db()
            self.assertEqual(self.customer.full_name, 'Local Name')
            self.assertEqual(self.customer.version, 3)
            self.assertIn('preserved 1 locally edited', output.getvalue())

    def test_customer_history_uses_identity_and_retains_filter(self):
        from sales.models import Document
        other = Customer.objects.create(source_id='other', full_name=self.customer.full_name)
        for kind, state, section in [('sale','completed','history'),('sale','held','held'),('order','open','orders')]:
            wanted = Document.objects.create(kind=kind, status=state, customer=self.customer, customer_name='Old Snapshot')
            Document.objects.create(kind=kind, status=state, customer=other, customer_name=self.customer.full_name)
            response = self.client.get(f'/pos/{section}/', {'customer':self.customer.pk, 'period':'all'})
            self.assertEqual([obj.pk for obj in response.context['page']], [wanted.pk])
            self.assertContains(response, f'name="customer" value="{self.customer.pk}"')
            self.assertEqual(self.client.get(f'/pos/{section}/?customer=bad').status_code, 404)

    def test_new_forms_require_csrf(self):
        from django.test import Client, override_settings
        with override_settings(MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware']):
            client = Client(enforce_csrf_checks=True)
            self.assertEqual(client.post('/customers/new/', self.fields()).status_code, 403)
            self.assertEqual(client.post(f'/customers/{self.customer.pk}/edit/', self.fields()).status_code, 403)

    def test_navigation_and_quick_find(self):
        for area, target in [('customers','customers'), ('items','purchasing/items'), ('vendors','purchasing/vendors'), ('receipts','pos/history'), ('orders','pos/orders'), ('purchase-orders','purchasing/orders'), ('https://example.com','customers')]:
            response = self.client.get('/find/', {'area':area, 'q':'name & number'})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.url.startswith('/'+target+'/?'))
            self.assertIn('q=name+%26+number', response.url)
        for url in ['/', '/customers/', '/customers/new/']:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post('/find/').status_code, 405)
