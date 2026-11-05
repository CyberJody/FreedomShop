# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run setup on the command line, for installs without a browser at hand: the same questions as the wizard at
/setup/, checked by the same forms (shop_settings/setup_forms.py).

    manage.py setup --business "My Shop" --tax-rate 7 --tax-location "Springfield" --preset calendar \\
        --admin owner --admin-password-env OWNER_PASSWORD [--register register --register-password-env REG_PASSWORD] \\
        [--cashier "Pat:PAT_PIN" ...] [--printer-host 192.0.2.50] [--demo] [--live] --no-input

Passwords and PINs are read from environment variables named on the command line (or typed at a hidden prompt),
never taken as arguments, and never printed. Without --no-input, missing answers are asked for interactively.
Refuses to run once setup is done unless --force; with --force only what is given changes (the rest stays), and the
logins step is skipped when an admin already exists, as in the wizard.
"""
import getpass
import os
import sys

from django.core.exceptions import NON_FIELD_ERRORS
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.forms import BaseFormSet
from django.views.decorators.debug import sensitive_variables

from shop_settings import setup
from shop_settings.config import business, seed
from shop_settings.models import Business, TaxSettings
from shop_settings.setup_forms import (TAX_PRESETS, CashierFormSet, SetupAdminForm, SetupBusinessForm, SetupPrinterForm,
                                       SetupTaxForm, business_initial, tax_initial)

BUSINESS_OPTIONS = {'business': 'name', 'legal_name': 'legal_name', 'address': 'address_line1', 'address2': 'address_line2',
                    'city': 'city', 'region': 'region', 'postal_code': 'postal_code', 'country': 'country', 'phone': 'phone',
                    'email': 'email', 'website': 'website', 'time_zone': 'time_zone', 'currency': 'currency'}


def _errors(step, form):
    """A form's or formset's errors, one line each, with the field label (never the value typed)."""
    if isinstance(form, BaseFormSet):
        lines = [f'{step}: {error}' for error in form.non_form_errors()]
        for n, sub in enumerate(form.forms, 1):
            lines += _errors(f'cashier {n}', sub)
        return lines
    return [f'{step}: {error}' if field == NON_FIELD_ERRORS else f'{step}: {form.fields[field].label}: {error}'
            for field, errors in form.errors.items() for error in errors]


class Command(BaseCommand):
    help = 'First-run setup: business, sales tax, admin login, optional register login, PIN cashiers, receipt printer, demo data, live shop.'

    def add_arguments(self, parser):
        business_group = parser.add_argument_group('business')
        business_group.add_argument('--business', help='Business name (required the first time).')
        for option in ('legal-name', 'address', 'address2', 'city', 'region', 'postal-code', 'country', 'phone', 'email', 'website'):
            business_group.add_argument(f'--{option}')
        business_group.add_argument('--time-zone', help='e.g. America/New_York (takes effect when the app restarts).')
        business_group.add_argument('--currency', help='ISO code, e.g. USD.')
        tax_group = parser.add_argument_group('sales tax')
        charge = tax_group.add_mutually_exclusive_group()
        charge.add_argument('--tax-rate', help='Sales tax rate in percent, e.g. 7 or 8.875.')
        charge.add_argument('--no-tax', action='store_true', help='The shop does not charge sales tax.')
        tax_group.add_argument('--tax-location', help='Name of the place you collect tax for (receipts, Sales Tax report).')
        tax_group.add_argument('--preset', choices=sorted(TAX_PRESETS), help='ny = March-February, quarterly; calendar = January-December, quarterly.')
        tax_group.add_argument('--tax-year-start-month', type=int, help='1-12.')
        tax_group.add_argument('--filing-frequency', choices=[key for key, _label in TaxSettings.FREQUENCY])
        logins = parser.add_argument_group('logins (skipped when an admin already exists)')
        logins.add_argument('--admin', help='Admin username.')
        logins.add_argument('--admin-password-env', metavar='VAR', help='Environment variable holding the admin password.')
        logins.add_argument('--register', help='Shared register login name (optional).')
        logins.add_argument('--register-password-env', metavar='VAR', help='Environment variable holding the register password.')
        logins.add_argument('--cashier', action='append', default=[], metavar='NAME:VAR',
                            help='A PIN cashier: name, a colon, and the environment variable holding the 4-digit PIN. Repeatable.')
        printer = parser.add_argument_group('receipt printer (optional)')
        printer.add_argument('--printer-host', help='IP address or hostname of a network receipt printer.')
        printer.add_argument('--printer-name', default=None)
        printer.add_argument('--printer-port', type=int, default=None)
        parser.add_argument('--demo', action='store_true', help='Load the demo data (only into an empty shop).')
        live = parser.add_mutually_exclusive_group()
        live.add_argument('--live', action='store_true', help='This is the live shop: print without testing marks.')
        live.add_argument('--testing', action='store_true', help='Keep the testing marks on printouts (the default the first time).')
        parser.add_argument('--force', action='store_true', help='Run again after setup is done (changes only what is given).')
        parser.add_argument('--no-input', '--noinput', action='store_false', dest='interactive', help='Never prompt; fail on anything missing.')

    def ask(self, prompt, secret=False):
        if not self.interactive:
            return ''
        return (getpass.getpass(f'{prompt}: ') if secret else input(f'{prompt}: ')).strip()

    @sensitive_variables()
    def secret(self, var, prompt, problems, label):
        if var:
            value = os.environ.get(var, '')
            if not value:
                problems.append(f'{label}: the environment variable {var} is not set.')
            return value
        if self.interactive:
            first = getpass.getpass(f'{prompt}: ')
            if first and getpass.getpass(f'{prompt} again: ') != first:
                problems.append(f'{label}: the two entries do not match.')
            return first
        problems.append(f'{label}: give --{label.replace(" ", "-")}-env VAR (the environment variable holding it).')
        return ''

    @sensitive_variables()
    def handle(self, *args, **options):
        self.interactive = options['interactive'] and sys.stdin.isatty()
        row = business()
        if not row.pk:
            raise CommandError('The database is not ready. Run manage.py migrate first.')
        first_run = not row.setup_complete
        if not first_run and not options['force']:
            raise CommandError('Setup is already done. Change settings on the Settings screens, or run setup again with --force.')
        problems = []
        row = Business.objects.get(pk=row.pk)

        # 1. Business
        data = {name: getattr(row, name) for name in SetupBusinessForm._meta.fields if name != 'logo'}
        data.update(business_initial(row))
        if first_run and data.get('name') == seed().get('business', {}).get('name'):
            data['name'] = ''
        for option, field in BUSINESS_OPTIONS.items():
            if options.get(option) is not None:
                data[field] = options[option]
        if not data.get('name'):
            data['name'] = self.ask('Business name')
        business_form = SetupBusinessForm(data, instance=row)

        # 2. Sales tax
        values = tax_initial()
        if options['no_tax']:
            values.update(charge_tax=False)
        elif options['tax_rate'] is not None:
            values.update(charge_tax=True, default_rate=options['tax_rate'])
        elif first_run:
            answer = self.ask('Sales tax rate in percent (0 or blank = no sales tax)')
            if answer and answer != '0':
                values.update(charge_tax=True, default_rate=answer)
            elif self.interactive:
                values.update(charge_tax=False)
            else:
                problems.append('sales tax: give --tax-rate RATE or --no-tax.')
        if options['preset']:
            values.update(TAX_PRESETS[options['preset']][1])
        for option, field in (('tax_location', 'location_name'), ('tax_year_start_month', 'tax_year_start_month'),
                              ('filing_frequency', 'filing_frequency')):
            if options[option] is not None:
                values[field] = options[option]
        tax_form = SetupTaxForm({key: value for key, value in values.items() if value is not None})

        # 3. Logins
        admin_form = cashier_formset = None
        if setup.admin_exists():
            logins_note = 'Logins: an admin already exists, so this step was skipped.'
        else:
            username = options['admin'] or self.ask('Admin username')
            if not username:
                problems.append('admin: give --admin USERNAME and --admin-password-env VAR.')
            password = self.secret(options['admin_password_env'], 'Admin password', problems, 'admin password')
            login_data = dict(username=username, password1=password, password2=password)
            if options['register']:
                register_password = self.secret(options['register_password_env'], 'Register password', problems, 'register password')
                login_data.update(register_username=options['register'], register_password1=register_password,
                                  register_password2=register_password)
            admin_form = SetupAdminForm(login_data)
            cashier_data = {'cashier-TOTAL_FORMS': str(len(options['cashier'])), 'cashier-INITIAL_FORMS': '0'}
            for n, entry in enumerate(options['cashier']):
                name, _colon, var = entry.rpartition(':')
                if not name or not var:
                    problems.append(f'cashier {n + 1}: write it as NAME:VAR (VAR = the environment variable holding the PIN).')
                    continue
                pin = os.environ.get(var, '')
                if not pin:
                    problems.append(f'cashier {n + 1}: the environment variable {var} is not set.')
                cashier_data.update({f'cashier-{n}-name': name, f'cashier-{n}-pin': pin})
            cashier_formset = CashierFormSet(cashier_data, prefix='cashier')
            logins_note = None

        # 4. Receipt printer
        printer_form = None
        if options['printer_host']:
            instance = setup.receipt_printer()
            printer_form = SetupPrinterForm({'name': options['printer_name'] or instance.name or 'Receipt printer',
                                             'host': options['printer_host'], 'port': options['printer_port'] or instance.port or 9100},
                                            instance=instance)

        # 5. Demo data
        if options['demo']:
            refusal = setup.catalog_in_use()
            if refusal:
                problems.append(f'demo data: {refusal}')

        checks = [('business', business_form), ('sales tax', tax_form), ('admin', admin_form), ('cashiers', cashier_formset),
                  ('printer', printer_form)]
        for step, form in checks:
            if form is not None and not form.is_valid():
                problems += _errors(step, form)
        if problems:
            raise CommandError('Setup was not saved:\n  ' + '\n  '.join(dict.fromkeys(problems)))

        with transaction.atomic():
            saved = setup.save_business(business_form)
            config = setup.save_tax(tax_form.cleaned_data)
            if admin_form is not None:
                setup.create_logins(admin_form, cashier_formset)
            printer = setup.save_printer(printer_form) if printer_form is not None else None
            counts = setup.load_demo() if options['demo'] else None
            live = True if options['live'] else False if (options['testing'] or first_run) else saved.live
            setup.finish(live)

        out = [f'Business: {saved.name}' + (f' (time zone {saved.time_zone}; takes effect when the app restarts)' if saved.time_zone else '')]
        if config.charge_tax:
            out.append(f'Sales tax: {config.default_location.name} {config.line_rate_text}%, tax year from month '
                       f'{config.tax_year_start_month}, filed {config.filing_frequency}')
        else:
            out.append('Sales tax: not charged')
        if admin_form is not None:
            cashiers = len(cashier_formset.cashiers())
            out.append('Logins: 1 admin' + (', 1 register login' if admin_form.cleaned_data.get('register_username') else '') +
                       f', {cashiers} PIN cashier{"" if cashiers == 1 else "s"} created')
        else:
            out.append(logins_note)
        if printer is not None:
            out.append(f'Receipt printer: {printer.name} at {printer.host}:{printer.port}')
        if counts:
            out.append('Demo data: {departments} departments, {items} items, {customers} customers, {vendors} vendors'.format(**counts))
        out.append('Live shop: yes (no testing marks)' if live else 'Live shop: not yet (printouts carry testing marks)')
        out.append('Setup is done.')
        self.stdout.write('\n'.join(out))
