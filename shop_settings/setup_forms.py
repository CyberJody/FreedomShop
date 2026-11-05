# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-run setup forms, shared by the wizard (/setup/) and `manage.py setup` so both check the same things."""
import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator

from accounts.models import Cashier
from purchasing.forms import TaxRateField
from sales.models import Printer
from sales.printer_views import PrinterForm
from .config import DEFAULT_TIME_ZONE, time_zone_names
from .forms import RATE, BusinessForm
from .models import MONTHS, TaxLocation, TaxSettings

# Tax presets: the tax year and filing frequency they fill in (the rate and location name stay as typed).
TAX_PRESETS = {
    'ny': ('New York (March–February, quarterly)', dict(tax_year_start_month=3, filing_frequency='quarterly')),
    'calendar': ('Calendar year, quarterly', dict(tax_year_start_month=1, filing_frequency='quarterly')),
}
CASHIER_ROWS = 4


class SetupBusinessForm(BusinessForm):
    time_zone = forms.ChoiceField(initial=DEFAULT_TIME_ZONE, help_text='Dates and times on receipts and reports. Takes effect when the app restarts.')

    class Meta(BusinessForm.Meta):
        fields = ['name', 'legal_name', 'address_line1', 'address_line2', 'city', 'region', 'postal_code', 'country',
                  'phone', 'email', 'website', 'logo', 'time_zone', 'currency']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['time_zone'].choices = [(name, name.replace('_', ' ')) for name in time_zone_names()]
        self.fields['name'].label = 'Business name'
        self.fields['currency'].widget.attrs.update(size=4, style='text-transform:uppercase')

    def clean_currency(self):
        code = self.cleaned_data['currency'].strip().upper()
        if not re.fullmatch(r'[A-Z]{3}', code):
            raise forms.ValidationError('Enter a three-letter currency code such as USD.')
        return code


class SetupTaxForm(forms.Form):
    charge_tax = forms.BooleanField(required=False, label='Do you charge sales tax?')
    default_rate = TaxRateField(label='Sales tax rate (%)', required=False, help_text='e.g. 7 or 8.875.', **RATE)
    location_name = forms.CharField(label='Tax location name', max_length=100, required=False,
                                    help_text='Your town, county or state, as it should read on receipts and the Sales Tax report.')
    tax_year_start_month = forms.TypedChoiceField(label='Tax year starts in', choices=MONTHS, coerce=int, initial=1)
    filing_frequency = forms.ChoiceField(label='You file sales tax', choices=TaxSettings.FREQUENCY, initial='quarterly')

    def clean_location_name(self):
        return self.cleaned_data['location_name'].strip()

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        if data.get('charge_tax'):
            if data.get('default_rate') is None:
                self.add_error('default_rate', 'Enter the sales tax rate you charge.')
            if not data.get('location_name'):
                self.add_error('location_name', 'Name the place you collect tax for.')
        else:
            data['default_rate'] = 0
            data['location_name'] = data.get('location_name') or TaxSettings.objects.values_list('default_location__name', flat=True).first() or 'Local'
        name = data.get('location_name')
        if name and TaxLocation.objects.filter(name__iexact=name, is_exempt=True).exists():
            self.add_error('location_name', 'That is the name of the tax-exempt location; choose another.')
        return data


class SetupAdminForm(forms.Form):
    username = forms.CharField(label='Admin username', max_length=150, validators=[UnicodeUsernameValidator()])
    password1 = forms.CharField(label='Password', strip=False, widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    password2 = forms.CharField(label='Password again', strip=False, widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    register_username = forms.CharField(label='Register login name', max_length=150, required=False, validators=[UnicodeUsernameValidator()],
                                        help_text='Optional: one shared login for the register; staff then pick their name and PIN.')
    register_password1 = forms.CharField(label='Register password', strip=False, required=False,
                                         widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    register_password2 = forms.CharField(label='Register password again', strip=False, required=False,
                                         widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    def _new_username(self, field):
        name = self.cleaned_data[field].strip()
        if name and get_user_model().objects.filter(username__iexact=name).exists():
            raise forms.ValidationError('A login with this name already exists.')
        return name

    def clean_username(self):
        return self._new_username('username')

    def clean_register_username(self):
        return self._new_username('register_username')

    def _check_password(self, user_field, first, second):
        password, again = self.cleaned_data.get(first), self.cleaned_data.get(second)
        if not password:
            self.add_error(first, 'Enter a password.')
        elif password != again:
            self.add_error(second, 'The two passwords do not match.')
        else:
            try:
                validate_password(password, get_user_model()(username=self.cleaned_data.get(user_field, '')))
            except forms.ValidationError as exc:
                self.add_error(first, exc)

    def clean(self):
        data = super().clean()
        if 'username' in data:
            self._check_password('username', 'password1', 'password2')
        register = data.get('register_username')
        if register:
            if register.casefold() == data.get('username', '').casefold():
                self.add_error('register_username', 'The register login needs a name of its own.')
            else:
                self._check_password('register_username', 'register_password1', 'register_password2')
        elif data.get('register_password1') or data.get('register_password2'):
            self.add_error('register_username', 'Enter a name for the register login, or leave its password blank.')
        return data


class CashierForm(forms.Form):
    name = forms.CharField(label='Cashier name', max_length=100, required=False)
    pin = forms.CharField(label='4-digit PIN', required=False, strip=False,
                          widget=forms.PasswordInput(attrs={'inputmode': 'numeric', 'maxlength': 4, 'autocomplete': 'off'}))

    def clean(self):
        data = super().clean()
        name, pin = data.get('name', '').strip(), data.get('pin', '')
        data['name'] = name
        if name and not re.fullmatch(r'[0-9]{4}', pin):
            self.add_error('pin', 'A PIN is four digits.')
        elif pin and not name:
            self.add_error('name', 'Enter the cashier’s name.')
        elif name and Cashier.objects.filter(name__iexact=name).exists():
            self.add_error('name', 'A cashier with this name already exists.')
        return data


class BaseCashierFormSet(forms.BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        names, pins = set(), set()
        for form in self.forms:
            name, pin = form.cleaned_data.get('name'), form.cleaned_data.get('pin')
            if not name:
                continue
            if name.casefold() in names:
                raise forms.ValidationError('Each cashier needs a different name.')
            if pin in pins:
                raise forms.ValidationError('Each cashier needs a different PIN.')
            names.add(name.casefold())
            pins.add(pin)

    def cashiers(self):
        """(name, PIN) for every filled-in row."""
        return [(form.cleaned_data['name'], form.cleaned_data['pin']) for form in self.forms if form.cleaned_data.get('name')]


CashierFormSet = forms.formset_factory(CashierForm, formset=BaseCashierFormSet, extra=CASHIER_ROWS, max_num=20)


class SetupPrinterForm(forms.ModelForm):
    connected = forms.BooleanField(required=False, label='I have a printer connected')

    class Meta:
        model = Printer
        fields = ['name', 'host', 'port']
        help_texts = {'host': 'The receipt printer’s IP address on your network, e.g. 192.0.2.50.',
                      'port': 'Network receipt printers listen on 9100.'}

    clean_host = PrinterForm.clean_host

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields['name'].initial = 'Receipt printer'


class SetupLiveForm(forms.Form):
    live = forms.BooleanField(required=False, label='This is my live shop: print receipts and orders without the testing bars',
                              help_text='Leave it unticked while you try things out; tick it in setup later when you open.')


def business_initial(row):
    """Wizard starting values: a new shop's placeholder name starts blank, the time zone at the default."""
    from .config import seed
    initial = {'time_zone': row.time_zone or DEFAULT_TIME_ZONE, 'version': row.version}
    if not row.setup_complete and row.name == seed().get('business', {}).get('name'):
        initial['name'] = ''
    return initial


def tax_initial():
    from .config import tax
    config = tax()
    return dict(charge_tax=config.charge_tax, default_rate=config.default_rate,
                location_name=config.default_location.name, tax_year_start_month=config.tax_year_start_month,
                filing_frequency=config.filing_frequency)

