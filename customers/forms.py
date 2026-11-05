# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal
from django import forms
from .models import tax_location_choices, tax_location_for, Customer


class CustomerForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    account_limit = forms.DecimalField(label='Account Limit ($)', required=False, min_value=0, max_digits=12, decimal_places=2)

    def clean_account_limit(self):
        return self.cleaned_data.get('account_limit') or Decimal('0')

    class Meta:
        model = Customer
        fields = ['first_name', 'last_name', 'title', 'company', 'customer_number', 'phone', 'mobile',
                  'email', 'street', 'city', 'state', 'postal_code', 'alternate_phone', 'alternate_phone_2',
                  'alternate_contact', 'notes', 'customer_type', 'track_as_company', 'tax_location', 'tax_exempt', 'tax_rate',
                  'accept_checks', 'email_opt_in', 'do_not_mail', 'active', 'use_with_quickbooks', 'charge_account',
                  'account_limit', 'version']
        labels = {'first_name': 'First Name', 'last_name': 'Last Name',
                  'mobile': 'Mobile', 'customer_number': 'Customer #', 'alternate_phone_2': 'Additional phone',
                  'email_opt_in': 'OK to email', 'do_not_mail': 'Do not use billing address for shipping',
                  'tax_rate': 'Customer tax rate override (%)', 'tax_location': 'Tax Location', 'tax_exempt': 'Tax Exempt',
                  'use_with_quickbooks': 'Use with QuickBooks', 'charge_account': 'Charge Account', 'account_limit': 'Account Limit ($)'}
        widgets = {'street': forms.Textarea(attrs={'rows': 3})}

    tax_location = forms.ChoiceField(label='Tax Location', choices=tax_location_choices, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.initial['charge_account'] = True
            self.initial['account_limit'] = None
        # Blank on imported customers means the default location; show what the checkbox says.
        if not self.is_bound:
            self.initial['tax_location'] = tax_location_for(self.instance.tax_exempt if self.instance.pk else self.initial.get('tax_exempt', False))
        # the Customer # is QB's number or the one the app assigns on the first save; it never changes here.
        self.fields['customer_number'].disabled = True
        if not self.instance.pk:
            self.fields['customer_number'].widget.attrs['placeholder'] = 'Assigned when saved'
        self.fields['tax_location'].widget.attrs.update({'data-tax-location': '1', 'data-default-location': tax_location_for(False),
                                                          'data-exempt-location': tax_location_for(True)})
        self.fields['tax_exempt'].widget.attrs['data-tax-exempt'] = '1'

    def clean(self):
        data = super().clean()
        # One rule, whatever the browser sent: the Tax Exempt checkbox decides the location.
        data['tax_location'] = tax_location_for(data.get('tax_exempt'))
        if data.get('track_as_company') and not data.get('company'):
            self.add_error('company', 'Enter a company name when tracking as a company.')
        if data.get('tax_rate') is not None and not 0 <= data['tax_rate'] <= 100:
            self.add_error('tax_rate', 'Enter a rate from 0 to 100.')
        if not data.get('first_name') and not data.get('last_name') and not data.get('company'):
            raise forms.ValidationError('Enter a first name, last name, or company name.')
        if self.instance.full_name and not self.instance.composed_name and not (data.get('first_name') or data.get('last_name')):
            raise forms.ValidationError('Enter the existing customer name in the separate first and last name fields before saving.')
        return data

    def save(self, commit=True):
        customer = super().save(commit=False)
        customer.full_name = customer.composed_name
        if commit:
            customer.save()
        return customer

from django.forms import inlineformset_factory, BaseInlineFormSet
from .models import CustomerAddress


class AddressFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        defaults = sum(bool(f.cleaned_data.get('is_default')) for f in self.forms
                       if hasattr(f, 'cleaned_data') and f.cleaned_data and not f.cleaned_data.get('DELETE'))
        if defaults > 1:
            raise forms.ValidationError('Choose only one default shipping address.')


CustomerAddressFormSet = inlineformset_factory(Customer, CustomerAddress,
    fields=['label', 'address', 'is_default'], formset=AddressFormSet,
    widgets={'address': forms.Textarea(attrs={'rows': 3})},
    extra=1, max_num=10, validate_max=True, absolute_max=10, can_delete=True)
