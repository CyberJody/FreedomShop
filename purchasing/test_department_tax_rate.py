# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): the Department List's Tax Code shows the rate the department gives its items."""
from decimal import Decimal as D

from django.test import TestCase

from purchasing.models import Department


class DepartmentTaxRateOnList(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.taxable = Department.objects.create(code='TST', name='TEST Tables')            # model defaults: Taxable Sales, 7
        cls.non = Department.objects.create(code='TSN', name='TEST Gift Cards', tax_category='Non', tax_rate=D('0'))
        cls.odd = Department.objects.create(code='TSO', name='TEST Other Town', tax_rate=D('8.875'))

    def test_tax_label_is_the_code_and_the_departments_rate(self):
        self.assertEqual(self.taxable.tax_label, 'Taxable Sales · 7%')
        self.assertEqual(self.non.tax_label, 'Non · 0%')
        self.assertEqual(self.odd.tax_label, 'Taxable Sales · 8.875%')

    def test_department_list_and_its_scroll_rows_show_the_rate(self):
        page = self.client.get('/purchasing/departments/', {'view': 'pages'})
        self.assertContains(page, '<th>Tax Code</th>')
        self.assertContains(page, '<td>Taxable Sales · 7%</td>')
        self.assertContains(page, '<td>Non · 0%</td>')
        rows = self.client.get('/purchasing/departments/', {'format': 'rows', 'offset': 0}).json()['rows']
        cells = {row['name']: row['cells'][3] for row in rows}
        self.assertEqual(cells['TEST Tables'], 'Taxable Sales · 7%')
        self.assertEqual(cells['TEST Other Town'], 'Taxable Sales · 8.875%')

    def test_rate_shown_is_what_a_new_item_in_the_department_gets(self):
        """The list's rate and the Add Item form's department default come from the same field (department source)."""
        page = self.client.get('/purchasing/items/new/')
        taxes = page.context['department_taxes']
        self.assertEqual(taxes[str(self.odd.pk)], {'category': 'Taxable Sales', 'rate': '8.875'})
        self.assertEqual(taxes[str(self.non.pk)], {'category': 'Non', 'rate': '0'})
