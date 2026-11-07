# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account maintenance fields share the setup wizard's username and password rules."""
from copy import deepcopy
import re

from django import forms
from django.contrib.auth import get_user_model

from shop_settings.setup_forms import CashierForm as SetupCashierForm, SetupAdminForm
from .models import Cashier

ROLES = [('Admin', 'Admin'), ('register', 'Register')]


class AddLoginForm(SetupAdminForm):
    role = forms.ChoiceField(choices=ROLES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('register_username', 'register_password1', 'register_password2'):
            self.fields.pop(name)
        self.fields['username'].label = 'Username'


class RenameLoginForm(forms.Form):
    username = deepcopy(SetupAdminForm.base_fields['username'])

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Username'

    def clean_username(self):
        name = self.cleaned_data['username']
        if get_user_model().objects.filter(username__iexact=name).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError('A login with this name already exists.')
        return name


class RoleForm(forms.Form):
    role = forms.ChoiceField(choices=ROLES)


class ConfirmForm(forms.Form):
    pass


def require_reauthentication(form):
    form.fields['current_password'] = forms.CharField(
        label='Your current admin password', strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}))
    return form


class CashierNameForm(forms.Form):
    name = deepcopy(SetupCashierForm.base_fields['name'])

    def __init__(self, *args, cashier=None, **kwargs):
        self.cashier = cashier
        super().__init__(*args, **kwargs)
        self.fields['name'].required = True

    def clean_name(self):
        name = self.cleaned_data['name']
        existing = Cashier.objects.all()
        if self.cashier:
            existing = existing.exclude(pk=self.cashier.pk)
        # Historical names stay reserved, including those of disabled cashiers.
        normalized_name = name.casefold()
        for current_name, previous_names in existing.values_list('name', 'previous_names'):
            if any(alias.casefold() == normalized_name for alias in (current_name, *previous_names)):
                raise forms.ValidationError('A cashier with this name already exists.')
        return name


class PinForm(forms.Form):
    pin = deepcopy(SetupCashierForm.base_fields['pin'])
    pin_again = deepcopy(SetupCashierForm.base_fields['pin'])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('pin', 'pin_again'):
            field = self.fields[name]
            field.required = True
            field.label = '4–6 digit PIN' if name == 'pin' else 'PIN again'
            field.widget.attrs.update(maxlength=6, pattern='[0-9]{4,6}')

    def clean_pin(self):
        pin = self.cleaned_data['pin']
        if not re.fullmatch(r'[0-9]{4,6}', pin):
            raise forms.ValidationError('A PIN must contain 4–6 digits (0–9).')
        return pin

    def clean(self):
        data = super().clean()
        if data.get('pin') and data.get('pin') != data.get('pin_again'):
            self.add_error('pin_again', 'The two PINs do not match.')
        return data


class AddCashierForm(CashierNameForm, PinForm):
    pass
