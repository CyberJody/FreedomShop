# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax Settings: the screen (admin only), the seed and migration rules, and a second configuration ("Downtown 8.875 /
Exempt / January", set through the screen) flowing to receipts, orders, the Sales Tax report and the period picker.
The older tests run on the Springfield configuration (config/test_settings.py SHOP_SETTINGS_SEED). TEST data only."""
import uuid
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register
from customers.forms import CustomerForm
from customers.models import Customer, tax_location_for
from purchasing.models import Department
from reports import sales_tax, sales_tax_exports, tax_periods
from sales.models import Item
from sales.services import save_document
from . import config
from .models import TaxCategory, TaxLocation, TaxSettings

FULL_MIDDLEWARE = ['django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
                   'django.contrib.auth.middleware.AuthenticationMiddleware', 'accounts.middleware.RequireLoginMiddleware']


class SettingsCase(TestCase):
    def setUp(self):
        config.clear_cache()
        self.addCleanup(config.clear_cache)   # a test's own rows roll back; its cached snapshot must not outlive it


class SeedTests(SettingsCase):
    def test_tests_run_on_the_springfield_configuration(self):
        tax = config.tax()
        self.assertEqual((tax.default_location.name, tax.default_location.rate, tax.default_location.section_label), ('Springfield', D('7'), 'Springfield, IL'))
        self.assertEqual((tax.exempt_location.name, tax.tax_year_start_month, tax.line_rate), ('Exempt', 3, D('7')))
        self.assertEqual([(c.name, c.taxable) for c in tax.categories], [('Taxable Sales', True), ('Non', False)])
        self.assertEqual(tax.estimate_fraction, '7/107')

    def test_a_new_shop_gets_neutral_defaults(self):
        TaxSettings.objects.all().delete()
        TaxLocation.objects.all().delete()
        TaxCategory.objects.all().delete()
        with override_settings(SHOP_SETTINGS_SEED=''):
            config.clear_cache()
            tax = config.tax()
        self.assertEqual((tax.default_location.name, tax.default_location.rate, tax.default_rate), ('Local', D('0'), D('0')))
        self.assertEqual((tax.exempt_location.name, tax.tax_year_start_month, tax.basis), ('Exempt', 1, 'cash'))
        self.assertEqual([(c.name, c.taxable) for c in tax.categories], [('Taxable', True), ('Non-taxable', False)])
        self.assertEqual(TaxSettings.objects.count(), 1)

    def test_seed_values_are_used_when_the_database_cannot_be_read(self):
        with mock.patch.object(config, '_read_tax', side_effect=AssertionError('no queries here')):
            config.clear_cache()
            self.assertEqual(config.tax().default_location.name, 'Springfield')
            self.assertNotIn('tax', config._cache)   # nothing cached from the fallback

    def test_saving_a_row_drops_the_cached_snapshot(self):
        self.assertEqual(config.tax().default_rate, D('7'))
        TaxSettings.objects.update(default_rate=D('6'))       # update sends no signal: still cached
        self.assertEqual(config.tax().default_rate, D('7'))
        row = TaxSettings.objects.get()
        row.save()
        self.assertEqual(config.tax().default_rate, D('6'))


class MigrationTests(SettingsCase):
    """shop_settings 0002: locations and the settings row from an existing database's own strings."""
    def run_migration(self):
        from importlib import import_module

        from django.apps import apps
        forwards = import_module('shop_settings.migrations.0002_locations_from_documents').forwards
        forwards(apps, mock.Mock(connection=connection))

    def clear_tax_rows(self):
        TaxSettings.objects.all().delete()
        TaxLocation.objects.all().delete()
        config.clear_cache()

    def test_locations_come_from_documents_and_customers(self):
        Department.objects.create(name='TEST Chairs', tax_rate=D('8.875'))
        Department.objects.create(name='TEST Cards', tax_category='Non', tax_rate=D('0'))
        Customer.objects.create(source_id='TEST-m1', full_name='TEST One', tax_location='Main Street')
        Customer.objects.create(source_id='TEST-m2', full_name='TEST Two', tax_location='Main Street')
        Customer.objects.create(source_id='TEST-m3', full_name='TEST Three', tax_location='Exempt', tax_exempt=True)
        Customer.objects.create(source_id='TEST-m4', full_name='TEST Four', tax_location='Harbor')
        self.clear_tax_rows()
        self.run_migration()
        rows = {loc.name: (loc.rate, loc.is_exempt) for loc in TaxLocation.objects.all()}
        self.assertEqual(rows, {'Main Street': (D('8.875'), False), 'Harbor': (D('8.875'), False), 'Exempt': (D('0'), True)})
        settings_row = TaxSettings.objects.get()
        self.assertEqual((settings_row.default_location.name, settings_row.default_rate), ('Main Street', D('8.875')))

    def test_an_exempt_location_is_added_when_none_is_named(self):
        Customer.objects.create(source_id='TEST-m5', full_name='TEST Five')
        self.clear_tax_rows()
        self.run_migration()
        self.assertEqual(sorted(TaxLocation.objects.values_list('name', 'is_exempt')), [('Exempt', True), ('Local', False)])

    def test_department_category_accepts_a_name_like_the_old_text_field(self):
        dept = Department.objects.create(name='TEST Gifts', tax_category='nontaxable', tax_rate=D('0'))
        self.assertEqual(dept.tax_category.name, 'Non')
        self.assertEqual(Department.objects.create(name='TEST Default').category_name, 'Taxable Sales')
        custom = Department.objects.create(name='TEST Food', tax_category='Prepared Food')
        self.assertTrue(TaxCategory.objects.get(name='Prepared Food').taxable)
        self.assertEqual(custom.tax_label, 'Prepared Food · 7%')


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class ScreenAccessTests(SettingsCase):
    def test_admin_only_like_server_status(self):
        sign_in_register(self.client)
        self.assertRedirects(self.client.get('/settings/tax/'), '/?admin=off', fetch_redirect_response=False)
        response = self.client.post('/settings/tax/', {'action': 'general'})
        self.assertEqual((response.status_code, response.json()['error']), (403, 'Sign in as an admin to use this.'))
        self.assertNotContains(self.client.get('/'), '/settings/tax/')
        sign_in_admin(self.client)
        page = self.client.get('/settings/tax/')
        self.assertContains(page, 'Tax Locations')
        self.assertContains(page, 'Tax Categories')
        self.assertContains(page, 'QuickBooks POS compatible')
        home = self.client.get('/')
        self.assertContains(home, '<a href="/printers/">Printers</a><a href="/settings/business/">Business Settings</a><a href="/settings/tax/">Tax Settings</a>')   # File menu
        self.assertIn({'label': 'Tax Settings', 'url': '/settings/tax/', 'icon': 'reports'}, home.context['report_tasks'])

    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=True)
    def test_signed_out_goes_to_sign_in(self):
        response = self.client.get('/settings/tax/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class ScreenTests(SettingsCase):
    def setUp(self):
        super().setUp()
        sign_in_admin(self.client)

    def general(self, **changes):
        row = TaxSettings.objects.get()
        data = dict(action='general', version=row.version, charge_tax='on', default_location=row.default_location_id, default_rate='7',
                    basis='cash', filing_frequency='quarterly', tax_year_start_month='3', estimate_rule='')
        data.update(changes)
        return self.client.post('/settings/tax/', data)

    def test_general_settings_save_with_a_version_check(self):
        self.assertRedirects(self.general(default_rate='6.5', filing_frequency='monthly'), '/settings/tax/?saved=general', fetch_redirect_response=False)
        row = TaxSettings.objects.get()
        self.assertEqual((row.default_rate, row.filing_frequency, row.version), (D('6.5'), 'monthly', 2))
        stale = self.general(version='1', default_rate='5')
        self.assertContains(stale, 'Someone changed the tax settings', status_code=400)
        exempt = TaxLocation.objects.get(is_exempt=True)
        self.assertEqual(self.general(default_location=exempt.pk).status_code, 400)   # the exempt location can't be the default

    def test_locations_add_retire_and_never_delete_in_use(self):
        response = self.client.post('/settings/tax/', {'action': 'add-location', 'new-loc-name': 'Harbor', 'new-loc-rate': '8',
                                                         'new-loc-label': '', 'new-loc-report_label': 'Harbor, NY', 'new-loc-sort': '2'})
        self.assertRedirects(response, '/settings/tax/?saved=location-added#locations', fetch_redirect_response=False)
        harbor = TaxLocation.objects.get(name='Harbor')
        self.assertContains(self.client.post('/settings/tax/', {'action': 'add-location', 'new-loc-name': 'harbor', 'new-loc-rate': '8', 'new-loc-sort': '0'}),
                            'already exists', status_code=400)
        Customer.objects.create(source_id='TEST-h', full_name='TEST Harbor', tax_location='Harbor')
        self.assertContains(self.client.post('/settings/tax/', {'action': 'location-delete', 'pk': harbor.pk}), 'can only be retired', status_code=400)
        self.client.post('/settings/tax/', {'action': 'location-retire', 'pk': harbor.pk})
        harbor.refresh_from_db()
        self.assertFalse(harbor.active)
        self.assertNotIn('Harbor', [loc.name for loc in config.tax().sections])
        springfield = TaxLocation.objects.get(name='Springfield')
        self.assertContains(self.client.post('/settings/tax/', {'action': 'location-retire', 'pk': springfield.pk}), 'default tax location', status_code=400)
        self.assertContains(self.client.post('/settings/tax/', {'action': 'location-retire', 'pk': TaxLocation.objects.get(is_exempt=True).pk}),
                            'only one', status_code=400)
        # The name never changes, even if the browser sends one.
        self.client.post('/settings/tax/', {'action': 'location', 'pk': springfield.pk, f'loc-{springfield.pk}-name': 'Elsewhere',
                                            f'loc-{springfield.pk}-rate': '7', f'loc-{springfield.pk}-label': 'DEMO', f'loc-{springfield.pk}-report_label': 'Springfield, IL',
                                            f'loc-{springfield.pk}-sort': '0'})
        springfield.refresh_from_db()
        self.assertEqual((springfield.name, springfield.label), ('Springfield', 'DEMO'))
        unused = TaxLocation.objects.create(name='TEST Unused', rate=1)
        self.client.post('/settings/tax/', {'action': 'location-delete', 'pk': unused.pk})
        self.assertFalse(TaxLocation.objects.filter(pk=unused.pk).exists())

    def test_categories_add_retire_and_keep_one_of_each(self):
        self.client.post('/settings/tax/', {'action': 'add-category', 'new-cat-name': 'Clothing', 'new-cat-taxable': 'on', 'new-cat-sort': '3'})
        clothing = TaxCategory.objects.get(name='Clothing')
        Department.objects.create(name='TEST Shirts', tax_category=clothing)
        self.assertContains(self.client.post('/settings/tax/', {'action': 'category-delete', 'pk': clothing.pk}), 'can only be retired', status_code=400)
        non = TaxCategory.objects.get(name='Non')
        self.assertContains(self.client.post('/settings/tax/', {'action': 'category-retire', 'pk': non.pk}), 'only non-taxable', status_code=400)
        self.client.post('/settings/tax/', {'action': 'category-retire', 'pk': clothing.pk})
        clothing.refresh_from_db()
        self.assertFalse(clothing.active)
        page = self.client.get('/purchasing/departments/new/')
        self.assertNotContains(page, '>Clothing<')   # retired: not offered to departments
        self.assertContains(page, '>Non<')


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class SecondConfigurationTests(SettingsCase):
    """Downtown 8.875 % as the default location, Exempt, tax year from January — entered on the screen."""
    def setUp(self):
        super().setUp()
        sign_in_admin(self.client)
        add = self.client.post('/settings/tax/', {'action': 'add-location', 'new-loc-name': 'Downtown', 'new-loc-rate': '8.875',
                                                    'new-loc-label': 'Downtown', 'new-loc-report_label': 'Downtown City', 'new-loc-sort': '0'})
        self.assertEqual(add.status_code, 302)
        downtown = TaxLocation.objects.get(name='Downtown')
        row = TaxSettings.objects.get()
        saved = self.client.post('/settings/tax/', dict(action='general', version=row.version, charge_tax='on', default_location=downtown.pk,
                                                        default_rate='8.875', basis='cash', filing_frequency='quarterly', tax_year_start_month='1'))
        self.assertEqual(saved.status_code, 302)
        retire = self.client.post('/settings/tax/', {'action': 'location-retire', 'pk': TaxLocation.objects.get(name='Springfield').pk})
        self.assertEqual(retire.status_code, 302)
        self.item = Item.objects.create(sku='TEST-DT-1', name='TEST Lamp', description='Lamp', price=D('100.00'), on_hand=5, tax_rate=None)
        self.customer = Customer.objects.create(source_id='TEST-dt', full_name='TEST Buyer')

    def sale(self, exempt=False, rate=None):
        line = dict(item_id=self.item.pk, quantity='1', price='100.00')
        if rate is not None:
            line['tax_rate'] = rate
        if exempt:
            self.customer.tax_exempt = True
            self.customer.save()
        return save_document(dict(action='complete', customer_id=self.customer.pk, tax_exempt=exempt, lines=[line],
                                  payments=[dict(method='cash', amount='200.00')]), 'sale', uuid.uuid4())

    def test_sale_takes_the_default_rate_and_location_and_prints_its_label(self):
        from sales.printing import Receipt, render_receipt
        receipt = self.sale()
        line = receipt.lines.get()
        self.assertEqual((line.tax_rate, receipt.tax_location, receipt.tax), (D('8.875'), 'Downtown', D('8.88')))
        drawn = []
        original = Receipt.text

        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(receipt)
        self.assertIn('Downtown   8.875 % Tax:', drawn)
        self.assertFalse(any('Tax' in text and 'Springfield' in text for text in drawn))
        other = self.sale(rate='7')
        drawn.clear()
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(other)
        self.assertIn('7 % Tax:', drawn)   # not the location's rate: just the rate

    def test_order_printout_names_the_location(self):
        from sales.letter_printing import Pages, render_sales_order
        order = save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=self.item.pk, quantity='1', price='100.00')]),
                              'order', uuid.uuid4())
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(order)
        texts = [step[3] for steps in pages for step in steps if step[0] == 'text']
        self.assertIn('Downtown 8.875 % Tax:', texts)

    def test_customers_and_new_items_follow_the_screen(self):
        self.assertEqual((tax_location_for(False), tax_location_for(True)), ('Downtown', 'Exempt'))
        self.assertEqual(CustomerForm().fields['tax_location'].choices, [('Downtown', 'Downtown (8.875%)'), ('Exempt', 'Exempt')])
        from purchasing.item_views import new_item_tax
        self.assertEqual(new_item_tax(), {'tax_category': 'Taxable Sales', 'tax_rate': D('8.875')})
        self.assertEqual(Department.objects.create(name='TEST New').tax_rate, D('8.875'))

    def test_sales_tax_report_sections_and_estimate_note(self):
        self.sale()
        self.sale(exempt=True)
        from django.utils import timezone
        today = timezone.localdate()
        report = sales_tax.build(tax_periods.resolve('custom', '', today, today, today), detailed=True)
        self.assertEqual([s.title for s in report.sections], ['Downtown City — Sales Tax 8.875%', 'Exempt — No Sales Tax'])
        downtown, exempt = report.sections
        self.assertEqual((downtown.tax, downtown.taxable, downtown.total, exempt.tax, exempt.total), (D('8.88'), D('100.00'), D('108.88'), D('0.00'), D('100.00')))
        notes = ' '.join(sales_tax_exports._notes(report))
        self.assertIn('exactly 8.875/108.875 for Downtown, zero for Exempt', notes)
        page = self.client.get('/reports/sales-tax/')
        self.assertContains(page, 'Downtown City (8.875%) and Exempt')
        TaxSettings.objects.update(estimate_rule='our own wording')
        TaxSettings.objects.get().save()
        self.assertIn('our own wording', ' '.join(sales_tax_exports._notes(report)))

    def test_period_picker_counts_quarters_from_january(self):
        self.assertEqual(tax_periods.quarter_of(date(2026, 11, 4)), (2026, 4))
        self.assertEqual(tax_periods.tax_year_range(2026), (date(2026, 1, 1), date(2026, 12, 31)))
        with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 11, 4)):
            page = self.client.get('/reports/sales-tax/', {'period': 'quarter', 'quarter': '2026-1', 'run': '1'})
        self.assertEqual(page.context['period'].label, 'Q1 2026 (Jan – Mar) · 1/1/2026 – 3/31/2026')
        self.assertEqual(page.context['quarters'][0], ('2026-4', 'Q4 2026 (Oct – Dec) · 10/1/2026 – 12/31/2026'))
        self.assertEqual(page.context['years'][0], ('2026', 'Sales tax year 2026 · 1/1/2026 – 12/31/2026'))

    def test_import_history_cutoff_rounds_to_the_configured_quarter(self):
        from sales.rebuild import history_cutoff
        self.assertEqual(history_cutoff(cutover=date(2026, 10, 15)), date(2021, 10, 1))   # March year: 9/1/2021

    def test_set_tag_uses_the_default_location(self):
        from tags.models import FurnitureSet, FurnitureSetLine
        from tags.sets import build
        tag_set = FurnitureSet.objects.create(name='TEST Set')
        FurnitureSetLine.objects.create(tag_set=tag_set, item=self.item, quantity=1)
        data = build(tag_set)
        self.assertEqual((data['tax_label'], data['tax']), ('Downtown   8.875 % Tax:', D('8.88')))
