# SPDX-License-Identifier: AGPL-3.0-or-later
"""An upgraded shop must choose its tax year explicitly; a fresh shop starts in January."""
from datetime import date
from importlib import import_module
from unittest.mock import Mock, patch

from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin
from customers.models import Customer
from reports import sales_tax, tax_periods
from shop_settings.config import clear_cache, tax
from shop_settings.models import TaxLocation, TaxSettings


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class TaxYearSetupTests(TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)

    def test_existing_database_migration_leaves_year_unset(self):
        Customer.objects.create(source_id='demo-upgrade', full_name='Demo Buyer', tax_location='Harbor')
        TaxSettings.objects.all().delete()
        TaxLocation.objects.all().delete()
        import_module('shop_settings.migrations.0002_locations_from_documents').forwards(apps, Mock(connection=connection))
        self.assertIsNone(TaxSettings.objects.get().tax_year_start_month)
        self.assertIsNone(tax().tax_year_start_month)

    def test_unset_year_requires_settings_before_report_exports_or_printing(self):
        row = TaxSettings.objects.get()
        row.tax_year_start_month = None
        row.save()
        sign_in_admin(self.client)
        with patch('reports.direct_print.print_report') as printer:
            for query in ('', '?run=1', '?format=pdf', '?format=xlsx'):
                self.assertRedirects(self.client.get('/reports/sales-tax/' + query), '/settings/tax/?required=tax-year', fetch_redirect_response=False)
            response = self.client.post('/reports/sales-tax/print/', {})
            self.assertEqual(response.status_code, 400)
            printer.assert_not_called()
        page = self.client.get('/settings/tax/?required=tax-year')
        self.assertContains(page, 'Choose the tax year start month')
        self.assertIsNone(page.context['general'].initial['tax_year_start_month'])
        data = {'action': 'general', 'version': row.version, 'charge_tax': 'on', 'default_location': row.default_location_id,
                'default_rate': '7', 'basis': 'cash', 'filing_frequency': 'quarterly', 'tax_year_start_month': ''}
        self.assertContains(self.client.post('/settings/tax/', data), 'This field is required.', status_code=400)
        self.assertEqual(self.client.post('/settings/tax/', {**data, 'tax_year_start_month': '3'}).status_code, 302)
        self.assertEqual(tax().tax_year_start_month, 3)
        self.assertEqual(self.client.get('/reports/sales-tax/?run=1').status_code, 200)

    def test_direct_tax_report_and_import_cutoff_refuse_an_unset_year(self):
        from django.core.exceptions import ImproperlyConfigured
        from sales.rebuild import history_cutoff
        row = TaxSettings.objects.get()
        row.tax_year_start_month = None
        row.save()
        period = tax_periods.resolve('today', '', date(2026, 11, 4))
        with self.assertRaisesMessage(ImproperlyConfigured, 'tax year start month'):
            sales_tax.build(period)
        with self.assertRaisesMessage(ImproperlyConfigured, 'tax year start month'):
            history_cutoff(cutover=date(2026, 10, 15))
