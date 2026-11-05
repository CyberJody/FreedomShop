# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): the Vendor List shows the vendor's contact, and Show Details adds what a clerk calling the vendor
needs (fax, account # terms, freight rate) when the vendor has it. 's "Vendor saved." note keeps working."""
from django.test import TestCase

from purchasing.models import Vendor


class VendorContactOnList(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ashery = Vendor.objects.create(code='TAO', company='TEST ASHERY OAK', contact='WAYNE TEST', phone='330-555-0100')
        cls.full = Vendor.objects.create(code='TFV', company='TEST FULL VENDOR', contact='PAT TEST', phone='518-555-0101',
                                         fax='518-555-0102', account_number='WC-1234', terms='Net 30', freight_rate='Prepaid')

    def test_details_text_lists_only_what_the_vendor_has(self):
        self.assertEqual(self.ashery.details_text, '')
        self.assertEqual(self.full.details_text, 'Fax: 518-555-0102\nAccount #: WC-1234\nTerms: Net 30\nFreight rate: Prepaid')

    def test_vendor_list_has_a_contact_column_and_the_details_on_the_row(self):
        page = self.client.get('/purchasing/vendors/', {'view': 'pages'})
        self.assertContains(page, '<th>Vendor Code</th><th>Company</th><th>Contact</th><th>Phone</th>', html=False)
        self.assertContains(page, '<td>WAYNE TEST</td><td>330-555-0100</td>', html=False)
        self.assertContains(page, f'<tr data-catalog-id="{self.full.pk}" data-details="Fax: 518-555-0102\nAccount #: WC-1234\nTerms: Net 30\nFreight rate: Prepaid">', html=False)
        self.assertContains(page, f'<tr data-catalog-id="{self.ashery.pk}">', html=False)
        self.assertEqual(page.context['list_scroll_config']['columns'], 10)

    def test_scroll_rows_carry_the_contact_and_details(self):
        rows = {row['name']: row for row in self.client.get('/purchasing/vendors/', {'format': 'rows', 'offset': 0}).json()['rows']}
        self.assertEqual(rows['TEST ASHERY OAK']['cells'][2:4], ['WAYNE TEST', '330-555-0100'])
        self.assertEqual(rows['TEST ASHERY OAK']['details'], '')
        self.assertIn('Account #: WC-1234', rows['TEST FULL VENDOR']['details'])

    def test_contact_is_searchable_and_sortable(self):
        page = self.client.get('/purchasing/vendors/', {'view': 'pages', 'q': 'wayne'})
        self.assertEqual([v.pk for v in page.context['page']], [self.ashery.pk])
        page = self.client.get('/purchasing/vendors/', {'view': 'pages', 'sort': '-contact'})
        self.assertEqual([v.pk for v in page.context['page']], [self.ashery.pk, self.full.pk])

    def test_vendor_saved_note_still_shows(self):
        page = self.client.get(f'/purchasing/vendors/{self.ashery.pk}/', {'saved': '1'})
        self.assertContains(page, 'Vendor saved.')
