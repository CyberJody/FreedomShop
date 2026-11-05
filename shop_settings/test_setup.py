# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run setup: the wizard's forms and screens, the gate, `manage.py setup`, demo data and the testing marks."""
import io
import logging
import os
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils.html import escape

from accounts.models import Cashier
from accounts.roles import ADMIN_GROUP, REGISTER_GROUP
from accounts.signin_helpers import AUTH_MIDDLEWARE, make_test_user, sign_in_admin, sign_in_register
from customers.models import Customer
from purchasing.models import Department, Vendor
from sales.models import Item, Printer
from sales.printing import testing_marks
from . import setup
from .config import apply_time_zone, business, clear_cache, seed, tax
from .models import Business, TaxLocation, TaxSettings
from .setup_forms import CashierFormSet, SetupAdminForm, SetupBusinessForm, SetupPrinterForm, SetupTaxForm

GATE = override_settings(MIDDLEWARE=AUTH_MIDDLEWARE + ['shop_settings.middleware.SetupGateMiddleware', 'accounts.middleware.RequireLoginMiddleware'],
                         POS_REQUIRE_LOGIN=True)
ADMIN_PASSWORD = 'Plum-Kettle-4471-TEST'
REGISTER_PASSWORD = 'Harbor-Lantern-9902-TEST'
PINS = ('4826', '7351')


PASSWORD_VALIDATORS = override_settings(AUTH_PASSWORD_VALIDATORS=[{'NAME': f'django.contrib.auth.password_validation.{name}'} for name in (
    'UserAttributeSimilarityValidator', 'MinimumLengthValidator', 'CommonPasswordValidator', 'NumericPasswordValidator')])


def finish_setup():
    setup.finish(False)


class SetupTestCase(TestCase):
    """The settings cache outlives a test's rolled-back rows, so every test starts and ends with it empty."""
    def setUp(self):
        super().setUp()
        clear_cache()
        self.addCleanup(clear_cache)


class CapturedLogs:
    """Every log record from every logger, at every level, while the block runs."""
    def __enter__(self):
        self.records = []
        self.handler = logging.Handler(logging.DEBUG)
        self.handler.emit = lambda record: self.records.append(record.getMessage() + ' ' + repr(record.__dict__))
        root = logging.getLogger()
        self.level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self.level)

    @property
    def text(self):
        return '\n'.join(self.records)


class BusinessFormTests(SetupTestCase):
    def form(self, **values):
        data = dict(name='Demo Home & Hardware', time_zone='America/New_York', currency='usd')
        data.update(values)
        return SetupBusinessForm(data, instance=Business.objects.get(pk=business().pk))

    def test_name_is_required(self):
        form = self.form(name='  ')
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)

    def test_currency_is_three_letters_and_upper_cased(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['currency'], 'USD')
        self.assertIn('currency', self.form(currency='dollars').errors)
        self.assertIn('currency', self.form(currency='U1D').errors)

    def test_time_zone_comes_from_the_list(self):
        self.assertIn('time_zone', self.form(time_zone='Mars/Olympus_Mons').errors)
        self.assertIn('time_zone', self.form(time_zone='US/Eastern').errors)   # legacy aliases are not offered
        self.assertTrue(self.form(time_zone='Europe/Dublin').is_valid())
        self.assertTrue(self.form(time_zone='UTC').is_valid())

    def test_email_is_checked(self):
        self.assertIn('email', self.form(email='not an address').errors)

    def test_placeholder_name_starts_blank_on_a_new_shop(self):
        from .setup_forms import business_initial
        row = Business.objects.get(pk=business().pk)
        row.name = seed()['business']['name']
        self.assertEqual(business_initial(row)['name'], '')
        row.setup_complete = True
        self.assertNotIn('name', business_initial(row))


class TaxFormTests(SetupTestCase):
    def test_charging_tax_needs_a_rate_and_a_location(self):
        form = SetupTaxForm(dict(charge_tax='on', default_rate='', location_name='', tax_year_start_month='1', filing_frequency='quarterly'))
        self.assertFalse(form.is_valid())
        self.assertIn('default_rate', form.errors)
        self.assertIn('location_name', form.errors)

    def test_rate_range(self):
        form = SetupTaxForm(dict(charge_tax='on', default_rate='101', location_name='Springfield', tax_year_start_month='1', filing_frequency='quarterly'))
        self.assertIn('default_rate', form.errors)
        form = SetupTaxForm(dict(charge_tax='on', default_rate='8.8755', location_name='Springfield', tax_year_start_month='1', filing_frequency='quarterly'))
        self.assertIn('default_rate', form.errors)

    def test_no_tax_means_a_zero_rate(self):
        form = SetupTaxForm(dict(default_rate='7', location_name='', tax_year_start_month='1', filing_frequency='annually'))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['default_rate'], 0)
        self.assertTrue(form.cleaned_data['location_name'])

    def test_month_and_frequency_choices(self):
        form = SetupTaxForm(dict(charge_tax='on', default_rate='7', location_name='Springfield', tax_year_start_month='13', filing_frequency='weekly'))
        self.assertIn('tax_year_start_month', form.errors)
        self.assertIn('filing_frequency', form.errors)

    def test_location_cannot_take_the_exempt_name(self):
        exempt = TaxLocation.objects.filter(is_exempt=True).first().name
        form = SetupTaxForm(dict(charge_tax='on', default_rate='7', location_name=exempt.upper(), tax_year_start_month='1', filing_frequency='quarterly'))
        self.assertIn('location_name', form.errors)


@PASSWORD_VALIDATORS
class AdminFormTests(SetupTestCase):
    def form(self, **values):
        data = dict(username='owner', password1=ADMIN_PASSWORD, password2=ADMIN_PASSWORD)
        data.update(values)
        return SetupAdminForm(data)

    def test_valid(self):
        self.assertTrue(self.form().is_valid(), self.form().errors)

    def test_passwords_must_match(self):
        self.assertIn('password2', self.form(password2='something-else-123').errors)

    def test_django_password_validators_run(self):
        self.assertIn('password1', self.form(password1='password', password2='password').errors)          # common
        self.assertIn('password1', self.form(password1='8812', password2='8812').errors)                  # short, numeric
        self.assertIn('password1', self.form(username='kettleplum', password1='kettleplum1', password2='kettleplum1').errors)  # like the name

    def test_username_rules(self):
        make_test_user('TEST Taken', ADMIN_GROUP)
        self.assertIn('username', self.form(username='test taken').errors)
        self.assertIn('username', self.form(username='no spaces!').errors)
        self.assertIn('username', self.form(username='').errors)

    def test_register_login_is_optional_but_complete(self):
        self.assertTrue(self.form(register_username='register', register_password1=REGISTER_PASSWORD, register_password2=REGISTER_PASSWORD).is_valid())
        self.assertIn('register_password1', self.form(register_username='register').errors)
        self.assertIn('register_username', self.form(register_password1=REGISTER_PASSWORD).errors)
        self.assertIn('register_username', self.form(register_username='OWNER', register_password1=REGISTER_PASSWORD,
                                                     register_password2=REGISTER_PASSWORD).errors)


class CashierFormTests(SetupTestCase):
    def formset(self, *rows):
        data = {'cashier-TOTAL_FORMS': str(len(rows)), 'cashier-INITIAL_FORMS': '0'}
        for n, (name, pin) in enumerate(rows):
            data.update({f'cashier-{n}-name': name, f'cashier-{n}-pin': pin})
        return CashierFormSet(data, prefix='cashier')

    def test_blank_rows_are_ignored(self):
        formset = self.formset(('Pat', '4826'), ('', ''))
        self.assertTrue(formset.is_valid())
        self.assertEqual(formset.cashiers(), [('Pat', '4826')])

    def test_pin_is_four_digits(self):
        for pin in ('482', '48261', 'abcd', ''):
            self.assertFalse(self.formset(('Pat', pin)).is_valid(), pin)

    def test_pin_needs_a_name(self):
        self.assertFalse(self.formset(('', '4826')).is_valid())

    def test_names_and_pins_are_distinct(self):
        self.assertFalse(self.formset(('Pat', '4826'), ('pat', '7351')).is_valid())
        self.assertFalse(self.formset(('Pat', '4826'), ('Sam', '4826')).is_valid())
        Cashier.objects.create(name='Sam', pin_hash='x')
        self.assertFalse(self.formset(('SAM', '7351'),).is_valid())


class PrinterFormTests(SetupTestCase):
    def test_host_and_port_are_checked(self):
        self.assertIn('host', SetupPrinterForm(dict(name='Receipt printer', host='not a host!', port='9100')).errors)
        self.assertFalse(SetupPrinterForm(dict(name='Receipt printer', host='192.0.2.50', port='70000')).is_valid())
        self.assertTrue(SetupPrinterForm(dict(name='Receipt printer', host='192.0.2.50', port='9100')).is_valid())

    def test_name_is_unique(self):
        Printer.objects.create(name='Front', host='127.0.0.1')
        self.assertIn('name', SetupPrinterForm(dict(name='Front', host='192.0.2.50', port='9100')).errors)


@GATE
class GateTests(SetupTestCase):
    def test_a_new_install_opens_on_setup(self):
        for path in ('/', '/pos/sale/new/', '/customers/', '/settings/business/', '/reports/'):
            response = self.client.get(path)
            self.assertRedirects(response, '/setup/', fetch_redirect_response=False, msg_prefix=path)
        self.assertRedirects(self.client.get('/setup/'), '/setup/business/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/setup/business/').status_code, 200)

    def test_open_paths(self):
        self.assertEqual(self.client.get('/health/').status_code, 200)
        self.assertEqual(self.client.get('/login/').status_code, 200)
        self.assertNotEqual(self.client.get('/static/app.css').get('Location'), '/setup/')

    def test_json_requests_get_a_json_answer(self):
        response = self.client.get('/pos/lookup/items/', HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['setup_url'], '/setup/')

    def test_signed_in_users_are_gated_too(self):
        sign_in_admin(self.client)
        self.assertRedirects(self.client.get('/'), '/setup/', fetch_redirect_response=False)

    def test_gate_opens_when_setup_is_done(self):
        finish_setup()
        self.assertRedirects(self.client.get('/'), '/login/?next=/', fetch_redirect_response=False)
        sign_in_admin(self.client)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_later_steps_wait_for_the_admin(self):
        for step in ('printer', 'demo', 'live'):
            self.assertRedirects(self.client.get(f'/setup/{step}/'), '/setup/admin/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/setup/tax/').status_code, 200)
        self.assertEqual(self.client.get('/setup/admin/').status_code, 200)

    def test_once_an_admin_exists_setup_needs_that_admin(self):
        make_test_user('TEST Admin', ADMIN_GROUP)
        for step in ('business', 'tax', 'admin', 'printer', 'demo', 'live'):
            self.assertRedirects(self.client.get(f'/setup/{step}/'), f'/login/?next=/setup/{step}/', fetch_redirect_response=False)
            response = self.client.post(f'/setup/{step}/', {'name': 'Hijacked'})
            self.assertEqual(response.status_code, 302)
        self.assertNotEqual(business().name, 'Hijacked')
        sign_in_register(self.client)
        self.assertRedirects(self.client.get('/setup/business/'), '/login/?next=/setup/business/', fetch_redirect_response=False)
        self.client.logout()
        sign_in_admin(self.client)
        self.assertEqual(self.client.get('/setup/business/').status_code, 200)

    def test_finished_setup_without_an_admin_is_locked(self):
        finish_setup()
        response = self.client.get('/setup/business/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'manage.py setup --force', status_code=403)

    def test_run_setup_again_link_is_for_admins_on_settings_screens(self):
        finish_setup()
        sign_in_admin(self.client)
        self.assertContains(self.client.get('/settings/business/'), 'href="/setup/">Run setup again')
        response = self.client.get('/setup/business/')
        self.assertContains(response, 'Run Setup Again')
        self.assertContains(response, f'value="{escape(business().name)}"')   # prefilled


@GATE
class WizardTests(SetupTestCase):
    def post_business(self, **values):
        data = dict(name='Demo Home & Hardware', legal_name='Demo Home & Hardware, LLC', address_line1='123 Main St', city='Springfield',
                    region='IL', postal_code='62701', phone='555-0100', email='hello@example.com', website='https://example.com',
                    time_zone='America/Chicago', currency='USD')
        data.update(values)
        return self.client.post('/setup/business/', data)

    def post_tax(self, **values):
        data = dict(charge_tax='on', default_rate='8.25', location_name='Springfield', tax_year_start_month='1', filing_frequency='quarterly')
        data.update(values)
        return self.client.post('/setup/tax/', data)

    def post_admin(self, **values):
        data = {'username': 'owner', 'password1': ADMIN_PASSWORD, 'password2': ADMIN_PASSWORD, 'register_username': 'register',
                'register_password1': REGISTER_PASSWORD, 'register_password2': REGISTER_PASSWORD,
                'cashier-TOTAL_FORMS': '4', 'cashier-INITIAL_FORMS': '0', 'cashier-0-name': 'Pat', 'cashier-0-pin': PINS[0],
                'cashier-1-name': 'Sam', 'cashier-1-pin': PINS[1]}
        data.update(values)
        return self.client.post('/setup/admin/', data)

    def test_whole_wizard(self):
        with CapturedLogs() as logs:
            self.assertContains(self.client.get('/setup/business/'), 'Step 1 of 6')
            self.assertRedirects(self.post_business(), '/setup/tax/', fetch_redirect_response=False)
            self.assertRedirects(self.post_tax(), '/setup/admin/', fetch_redirect_response=False)
            self.assertRedirects(self.post_admin(), '/setup/printer/', fetch_redirect_response=False)
            with mock.patch('shop_settings.setup_views.print_test_page') as send:
                response = self.client.post('/setup/printer/', dict(name='Front counter', host='192.0.2.50', port='9100', connected='on', action='test'))
            self.assertContains(response, 'Test page sent to Front counter')
            send.assert_called_once()
            self.assertFalse(Printer.objects.filter(name='Front counter').exists())   # a test page saves nothing
            self.assertRedirects(self.client.post('/setup/printer/', dict(name='Front counter', host='192.0.2.50', port='9100', action='save')),
                                 '/setup/demo/', fetch_redirect_response=False)
            self.assertContains(self.client.post('/setup/demo/', {'action': 'load'}), 'Demo data loaded: 3 departments, 12 items, 6 customers and 2 vendors.')
            self.assertFalse(business().setup_complete)
            self.assertRedirects(self.client.post('/setup/live/', {'live': 'on'}), '/', fetch_redirect_response=False)
        row = business()
        self.assertEqual((row.name, row.legal_name, row.city, row.time_zone, row.currency), ('Demo Home & Hardware', 'Demo Home & Hardware, LLC', 'Springfield', 'America/Chicago', 'USD'))
        self.assertTrue(row.setup_complete)
        self.assertTrue(row.live)
        self.assertFalse(testing_marks())
        config = tax()
        self.assertEqual((config.default_location.name, config.default_location.rate, config.default_rate, config.tax_year_start_month),
                         ('Springfield', Decimal('8.25'), Decimal('8.25'), 1))
        User = get_user_model()
        self.assertTrue(User.objects.get(username='owner').groups.filter(name=ADMIN_GROUP).exists())
        self.assertTrue(User.objects.get(username='register').groups.filter(name=REGISTER_GROUP).exists())
        self.assertEqual(sorted(Cashier.objects.values_list('name', flat=True)), ['Pat', 'Sam'])
        printer = Printer.objects.get(name='Front counter')
        self.assertEqual((printer.name, printer.kind, printer.host, printer.port), ('Front counter', 'receipt', '192.0.2.50', 9100))
        self.assertEqual(self.client.get('/').status_code, 200)   # signed in as the new admin, gate open
        for secret in (ADMIN_PASSWORD, REGISTER_PASSWORD, *PINS):
            self.assertNotIn(secret, logs.text)

    def test_admin_step_signs_the_admin_in_and_hides_credentials_on_errors(self):
        response = self.post_admin(password2='Different-Password-1')
        self.assertEqual(response.status_code, 400)
        for secret in (ADMIN_PASSWORD, REGISTER_PASSWORD, *PINS):
            self.assertNotContains(response, secret, status_code=400)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Cashier.objects.exists())
        self.post_admin()
        self.assertEqual(self.client.get('/setup/printer/').status_code, 200)
        response = self.client.get('/setup/admin/')
        self.assertContains(response, 'this step is skipped')
        self.assertNotContains(response, 'name="password1"')

    def test_pins_are_hashed(self):
        self.post_admin()
        for name, pin in (('Pat', PINS[0]), ('Sam', PINS[1])):
            cashier = Cashier.objects.get(name=name)
            self.assertNotIn(pin, cashier.pin_hash)
            self.assertTrue(check_password(pin, cashier.pin_hash))
        self.assertNotIn(ADMIN_PASSWORD, get_user_model().objects.get(username='owner').password)

    def test_pin_cashier_can_switch_in_under_the_register_login(self):
        self.post_admin()
        finish_setup()
        self.client.logout()
        self.client.force_login(get_user_model().objects.get(username='register'))
        response = self.client.post('/cashier/', {'cashier': Cashier.objects.get(name='Pat').pk, 'pin': PINS[0]})
        self.assertEqual(response.status_code, 200, response.content)

    def test_business_step_errors(self):
        response = self.post_business(name='', currency='X')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'This field is required', status_code=400)
        self.assertContains(response, 'three-letter currency code', status_code=400)

    def test_tax_presets_fill_in_without_saving(self):
        before = TaxSettings.objects.get().version
        response = self.client.post('/setup/tax/', dict(charge_tax='on', default_rate='8', location_name='Springfield', tax_year_start_month='1',
                                                        filing_frequency='monthly', preset='ny'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="3" selected>March</option>', html=True)
        self.assertContains(response, '<option value="quarterly" selected>Quarterly</option>', html=True)
        self.assertContains(response, 'value="Springfield"')
        self.assertEqual(TaxSettings.objects.get().version, before)
        response = self.client.post('/setup/tax/', dict(default_rate='8', location_name='Springfield', tax_year_start_month='3',
                                                        filing_frequency='monthly', preset='calendar'))
        self.assertContains(response, '<option value="1" selected>January</option>', html=True)
        self.assertNotContains(response, 'checked')

    def test_tax_step_reuses_rows(self):
        exempt = TaxLocation.objects.filter(is_exempt=True).count()
        self.post_tax()
        self.post_tax(default_rate='8.5')
        self.post_tax(location_name='springfield', default_rate='8.5')
        self.assertEqual(TaxLocation.objects.filter(is_exempt=False).count(), 1)   # the unused seed location was replaced
        self.assertEqual(TaxLocation.objects.filter(is_exempt=True).count(), exempt)
        self.assertEqual(TaxLocation.objects.get(is_exempt=False).rate, Decimal('8.5'))

    def test_tax_step_keeps_a_location_in_use(self):
        old = tax().default_location.name
        Customer.objects.create(source_id='TEST-1', full_name='TEST Customer', tax_location=old)
        self.post_tax()
        self.assertTrue(TaxLocation.objects.filter(name=old).exists())
        self.assertEqual(tax().default_location.name, 'Springfield')

    def test_no_sales_tax(self):
        self.assertRedirects(self.post_tax(charge_tax='', default_rate=''), '/setup/admin/', fetch_redirect_response=False)
        config = tax()
        self.assertFalse(config.charge_tax)
        self.assertEqual(config.line_rate, 0)

    def test_test_page_needs_the_printer_connected_box(self):
        self.post_admin()
        response = self.client.get('/setup/printer/')
        self.assertContains(response, 'id="send-test" disabled')
        with mock.patch('shop_settings.setup_views.print_test_page') as send:
            response = self.client.post('/setup/printer/', dict(name='Front counter', host='192.0.2.50', port='9100', action='test'))
        self.assertContains(response, 'before sending a test page', status_code=400)
        send.assert_not_called()

    def test_test_page_failure_is_shown(self):
        from sales.printing import PrintError
        self.post_admin()
        with mock.patch('shop_settings.setup_views.print_test_page', side_effect=PrintError('Could not reach Front counter.')):
            response = self.client.post('/setup/printer/', dict(name='Front counter', host='192.0.2.50', port='9100', connected='on', action='test'))
        self.assertContains(response, 'Could not reach Front counter.', status_code=400)

    def test_printer_and_demo_can_be_skipped(self):
        printers = list(Printer.objects.values_list('pk', 'version'))
        self.post_admin()
        self.assertContains(self.client.get('/setup/printer/'), 'href="/setup/demo/">Skip')
        self.assertContains(self.client.get('/setup/demo/'), 'href="/setup/live/"')
        self.client.post('/setup/live/', {})
        self.assertTrue(business().setup_complete)
        self.assertFalse(business().live)
        self.assertTrue(testing_marks())
        self.assertEqual(list(Printer.objects.values_list('pk', 'version')), printers)

    def test_demo_refused_when_the_shop_has_records(self):
        self.post_admin()
        Customer.objects.create(source_id='TEST-1', full_name='TEST Customer')
        self.assertContains(self.client.get('/setup/demo/'), 'already has customers')
        response = self.client.post('/setup/demo/', {'action': 'load'})
        self.assertContains(response, 'already has customers', status_code=400)
        self.assertFalse(Item.objects.exists())

    def test_run_again_is_prefilled(self):
        self.post_business()
        self.post_tax()
        self.post_admin()
        self.client.post('/setup/live/', {'live': 'on'})
        response = self.client.get('/setup/tax/')
        self.assertContains(response, 'value="Springfield"')
        self.assertContains(response, 'value="8.25"')
        self.assertContains(self.client.get('/setup/live/'), 'checked')
        self.assertContains(self.client.get('/setup/business/'), '<option value="America/Chicago" selected>', html=False)


class DemoDataTests(SetupTestCase):
    def test_loads_into_an_empty_catalog_with_the_shops_tax_setup(self):
        name = business().name
        counts = setup.load_demo()
        self.assertEqual(counts, {'departments': 3, 'items': 12, 'customers': 6, 'vendors': 2})
        self.assertEqual(business().name, name)   # the fixture's Business row is left out
        config = tax()
        for department in Department.objects.all():
            expected = config.line_rate if department.tax_category.taxable else 0
            self.assertEqual(department.tax_rate, expected)
            for item in department.item_set.all():
                self.assertEqual((item.tax_category, item.tax_rate), (department.category_name, expected))
        self.assertTrue(Department.objects.filter(tax_category__taxable=False).exists())
        self.assertTrue(all(item.price and item.order_cost is not None for item in Item.objects.all()))
        self.assertEqual(Customer.objects.filter(full_name='').count(), 0)

    def test_refused_when_anything_is_in_the_catalog(self):
        for make in (lambda: Customer.objects.create(source_id='TEST-1', full_name='TEST'),
                     lambda: Item.objects.create(sku='TEST-1', description='TEST'),
                     lambda: Department.objects.create(name='TEST'),
                     lambda: Vendor.objects.create(company='TEST')):
            with self.subTest():
                make()
                with self.assertRaises(setup.DemoRefused):
                    setup.load_demo()
                for model in (Customer, Item, Department, Vendor):
                    model.objects.all().delete()

    def test_fixture_is_made_up(self):
        import json
        records = json.loads(setup.DEMO_FIXTURE.read_text())
        self.assertEqual(next(r for r in records if r['model'] == 'shop_settings.business')['fields']['name'], 'Demo Home & Hardware')
        for record in records:
            fields = record['fields']
            if fields.get('email'):
                self.assertRegex(fields['email'], r'@example\.(com|net|org)$')
            if fields.get('phone'):
                self.assertRegex(fields['phone'], r'^555-01[0-9]{2}$')
        self.assertFalse(any(r['model'].startswith(('sales.document', 'sales.payment')) for r in records))   # no sales history


@PASSWORD_VALIDATORS
@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class SetupCommandTests(SetupTestCase):
    ENV = {'TEST_ADMIN_PW': ADMIN_PASSWORD, 'TEST_REGISTER_PW': REGISTER_PASSWORD, 'TEST_PIN_PAT': PINS[0], 'TEST_PIN_SAM': PINS[1]}

    def run_setup(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.ENV), CapturedLogs() as logs:
            try:
                call_command('setup', '--no-input', *args, stdout=out, stderr=err)
            finally:
                self.output = out.getvalue() + err.getvalue() + logs.text
        return out.getvalue()

    def assert_no_credentials(self, text):
        for secret in (ADMIN_PASSWORD, REGISTER_PASSWORD, *PINS, 'kestrel', 'heron'):
            self.assertNotIn(secret, text)

    def test_creates_everything(self):
        out = self.run_setup('--business', 'X', '--tax-rate', '7', '--tax-location', 'Springfield', '--preset', 'ny',
                             '--admin', 'kestrel', '--admin-password-env', 'TEST_ADMIN_PW',
                             '--register', 'heron', '--register-password-env', 'TEST_REGISTER_PW',
                             '--cashier', 'Pat:TEST_PIN_PAT', '--cashier', 'Sam:TEST_PIN_SAM',
                             '--printer-host', '127.0.0.1', '--time-zone', 'America/Denver', '--demo', '--live')
        row = business()
        self.assertEqual((row.name, row.time_zone, row.setup_complete, row.live), ('X', 'America/Denver', True, True))
        config = tax()
        self.assertEqual((config.default_location.name, config.default_location.rate, config.tax_year_start_month, config.filing_frequency),
                         ('Springfield', Decimal('7'), 3, 'quarterly'))
        User = get_user_model()
        admin = User.objects.get(username='kestrel')
        self.assertTrue(admin.check_password(ADMIN_PASSWORD))
        self.assertTrue(admin.groups.filter(name=ADMIN_GROUP).exists())
        self.assertTrue(User.objects.get(username='heron').groups.filter(name=REGISTER_GROUP).exists())
        self.assertTrue(check_password(PINS[0], Cashier.objects.get(name='Pat').pin_hash))
        self.assertTrue(Printer.objects.filter(kind='receipt', active=True, host='127.0.0.1').exists())
        self.assertEqual(Item.objects.count(), 12)
        self.assertIn('Setup is done.', out)
        self.assertIn('1 admin, 1 register login, 2 PIN cashiers created', out)
        self.assert_no_credentials(self.output)

    def test_minimal_example(self):
        self.run_setup('--business', 'X', '--tax-rate', '7', '--admin', 'kestrel', '--admin-password-env', 'TEST_ADMIN_PW')
        self.assertTrue(business().setup_complete)
        self.assertFalse(business().live)
        self.assertEqual(tax().default_rate, Decimal('7'))
        self.assert_no_credentials(self.output)

    def test_refuses_when_done_unless_forced(self):
        self.run_setup('--business', 'X', '--tax-rate', '7', '--admin', 'kestrel', '--admin-password-env', 'TEST_ADMIN_PW')
        with self.assertRaisesMessage(CommandError, 'Setup is already done'):
            self.run_setup('--business', 'Y', '--tax-rate', '8', '--admin', 'kestrel', '--admin-password-env', 'TEST_ADMIN_PW')
        self.assertEqual(business().name, 'X')
        out = self.run_setup('--force', '--business', 'Y')
        self.assertIn('an admin already exists', out)
        self.assertEqual(business().name, 'Y')
        self.assertEqual(tax().default_rate, Decimal('7'))   # not given: kept
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_force_keeps_live_unless_told(self):
        self.run_setup('--business', 'X', '--no-tax', '--admin', 'kestrel', '--admin-password-env', 'TEST_ADMIN_PW', '--live')
        self.run_setup('--force')
        self.assertTrue(business().live)
        self.run_setup('--force', '--testing')
        self.assertFalse(business().live)

    def test_missing_answers_save_nothing(self):
        with self.assertRaises(CommandError) as caught:
            self.run_setup('--admin', 'kestrel')
        message = str(caught.exception)
        self.assertIn('Business name', message)
        self.assertIn('--tax-rate RATE or --no-tax', message)
        self.assertIn('--admin-password-env', message)
        self.assertFalse(business().setup_complete)
        self.assertFalse(get_user_model().objects.exists())

    def test_unset_password_variable(self):
        with self.assertRaisesMessage(CommandError, 'NOT_SET_ANYWHERE is not set'):
            self.run_setup('--business', 'X', '--tax-rate', '7', '--admin', 'kestrel', '--admin-password-env', 'NOT_SET_ANYWHERE')
        self.assertFalse(get_user_model().objects.exists())

    def test_weak_password_and_bad_pin_are_refused_without_echo(self):
        self.ENV = {**self.ENV, 'TEST_WEAK': 'password', 'TEST_BAD_PIN': '12a4'}
        with self.assertRaises(CommandError) as caught:
            self.run_setup('--business', 'X', '--tax-rate', '7', '--admin', 'kestrel', '--admin-password-env', 'TEST_WEAK',
                           '--cashier', 'Pat:TEST_BAD_PIN')
        message = str(caught.exception)
        self.assertIn('This password is too common', message)
        self.assertIn('A PIN is four digits', message)
        self.assertNotIn('12a4', message + self.output)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Cashier.objects.exists())

    def test_demo_refused_on_a_non_empty_catalog(self):
        Item.objects.create(sku='TEST-1', description='TEST')
        with self.assertRaisesMessage(CommandError, 'already has items'):
            self.run_setup('--business', 'X', '--tax-rate', '7', '--admin', 'kestrel', '--admin-password-env', 'TEST_ADMIN_PW', '--demo')
        self.assertFalse(business().setup_complete)
        self.assertEqual(Item.objects.count(), 1)


class TestingMarksTests(SetupTestCase):
    def test_live_shop_turns_marks_off(self):
        self.assertTrue(testing_marks())
        setup.finish(True)
        self.assertFalse(testing_marks())

    @override_settings(PRINT_TESTING_MARKS=False)
    def test_setting_overrides(self):
        self.assertFalse(business().live)
        self.assertFalse(testing_marks())


class TimeZoneTests(SetupTestCase):
    def test_business_time_zone_becomes_time_zone_at_startup(self):
        from django.utils import timezone
        saved_tz = os.environ.get('TZ')
        self.addCleanup(timezone.get_default_timezone.cache_clear)
        self.addCleanup(lambda: os.environ.__setitem__('TZ', saved_tz) if saved_tz is not None else os.environ.pop('TZ', None))
        with override_settings(TIME_ZONE='America/New_York'):
            self.assertIsNone(apply_time_zone())   # blank: the settings file's value stays
            Business.objects.update(time_zone='America/Chicago')
            self.assertEqual(apply_time_zone(), 'America/Chicago')
            self.assertEqual(settings.TIME_ZONE, 'America/Chicago')
            self.assertEqual(str(timezone.get_default_timezone()), 'America/Chicago')
            Business.objects.update(time_zone='Nowhere/Special')
            settings.TIME_ZONE = 'America/New_York'
            self.assertIsNone(apply_time_zone())
        import time
        time.tzset()


class ExistingShopMigrationTests(SetupTestCase):
    def test_a_database_with_an_admin_skips_setup(self):
        import importlib

        from django.apps import apps
        migration = importlib.import_module('shop_settings.migrations.0006_business_setup')
        editor = mock.Mock(connection=mock.Mock(alias='default'))
        migration.mark_existing_shops(apps, editor)
        self.assertFalse(Business.objects.get().setup_complete)
        make_test_user('TEST Admin', ADMIN_GROUP)
        migration.mark_existing_shops(apps, editor)
        self.assertTrue(Business.objects.get().setup_complete)
