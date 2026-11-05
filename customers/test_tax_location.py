# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax Location and Tax Exempt stay in step: the checkbox decides; receipts record the location."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from sales.models import Document, Item
from sales.services import save_document

from .forms import CustomerForm
from .models import Customer


def form_data(customer, **changes):
    data = {f: getattr(customer, f) for f in CustomerForm.Meta.fields if f != 'version'}
    data = {k: ('' if v is None else v) for k, v in data.items()}
    data['version'] = customer.version
    data.update(changes)
    return {k: v for k, v in data.items() if v is not False}


class TaxLocationTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='t', first_name='COMMUNITY', last_name='GROUP', full_name='COMMUNITY GROUP')

    def test_checkbox_decides_location(self):
        form = CustomerForm(form_data(self.customer, tax_exempt=True, tax_location='Springfield'), instance=self.customer)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual((saved.tax_exempt, saved.tax_location), (True, 'Exempt'))
        form = CustomerForm(form_data(saved, tax_location='Exempt', tax_exempt=False), instance=saved)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual((saved.tax_exempt, saved.tax_location), (False, 'Springfield'))
        form = CustomerForm(form_data(saved, tax_location='Sales Tax Exempt'), instance=saved)
        self.assertFalse(form.is_valid())    # only Springfield / Exempt

    def test_edit_page_shows_dropdown_and_blank_means_springfield(self):
        page = self.client.get(f'/customers/{self.customer.pk}/edit/')
        self.assertContains(page, '<option value="Springfield" selected>')
        self.assertContains(page, 'data-tax-exempt')
        Customer.objects.filter(pk=self.customer.pk).update(tax_exempt=True, tax_location='Exempt')
        self.assertContains(self.client.get(f'/customers/{self.customer.pk}/edit/'), '<option value="Exempt" selected>')

    def test_receipts_record_location_and_sales_history_shows_it(self):
        item = Item.objects.create(sku='T-1', name='Chair', description='Chair', price=Decimal('100'), on_hand=5, tax_rate=7)
        numbers = {}
        for exempt in (False, True):
            token = str(uuid.uuid4())
            doc = save_document(dict(token=token, version=0, action='complete', customer_id=self.customer.pk, tax_exempt=exempt,
                                     lines=[dict(item_id=item.pk, quantity='1', price='100.00')],
                                     payments=[dict(method='cash', amount='107.00')]), 'sale', token)
            numbers[exempt] = doc
        self.assertEqual((numbers[False].tax_location, numbers[False].tax), ('Springfield', Decimal('7.00')))
        self.assertEqual((numbers[True].tax_location, numbers[True].tax), ('Exempt', Decimal('0')))
        # An older app receipt with no location falls back to its exempt flag.
        old = Document.objects.create(kind='sale', status='completed', customer_name='OLD', tax_exempt=True, total=1, subtotal=1, tax=0,
                                      completed_at=timezone.now() - timedelta(days=1))
        page = self.client.get('/pos/history/', {'sort': 'tax_area'})
        self.assertContains(page, '<th>Tax Location</th>')
        self.assertEqual([d.tax_area for d in page.context['page']], ['Exempt', 'Exempt', 'Springfield'])
        self.assertIn(old.pk, [d.pk for d in page.context['page']])
