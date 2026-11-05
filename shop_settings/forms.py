# SPDX-License-Identifier: AGPL-3.0-or-later
from django import forms

from purchasing.forms import TaxRateField
from .models import Business, TaxCategory, TaxLocation, TaxSettings

RATE = dict(max_digits=6, decimal_places=3, min_value=0, max_value=100)


def link_row_inputs(form):
    """A table row's inputs belong to the row's own <form id="{prefix}"> (a form can't wrap table cells)."""
    if form.instance.pk and form.prefix:
        for field in form.fields.values():
            field.widget.attrs['form'] = form.prefix


class GeneralForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    default_rate = TaxRateField(label='Default tax rate (%)', help_text='A new line with no rate from its customer or item gets this.', **RATE)

    class Meta:
        model = TaxSettings
        fields = ['charge_tax', 'default_location', 'default_rate', 'basis', 'filing_frequency', 'tax_year_start_month', 'estimate_rule']
        labels = {'charge_tax': 'Charge sales tax', 'default_location': 'Default tax location', 'basis': 'Tax basis',
                  'tax_year_start_month': 'Tax year starts in'}
        help_texts = {'charge_tax': 'Off: new lines and departments default to 0 %; rates already on items stay.',
                      'default_location': 'Sales and customers that are not tax-exempt get this location.',
                      'basis': 'The Sales Tax report always counts tax when money is received; this is recorded for your books.',
                      'tax_year_start_month': 'Quarters and tax years in the report period picker count from this month.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields['default_location']
        field.queryset = TaxLocation.objects.filter(is_exempt=False, active=True)
        field.required = True
        field.empty_label = None


class LocationForm(forms.ModelForm):
    rate = TaxRateField(label='Rate (%)', **RATE)

    class Meta:
        model = TaxLocation
        fields = ['name', 'rate', 'label', 'report_label', 'sort']
        labels = {'label': 'Receipt label', 'report_label': 'Report section'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Documents and customers keep the name as text, so it never changes once saved.
            self.fields['name'].disabled = True
            if self.instance.is_exempt:
                self.fields['rate'].disabled = True
        for name in ('label', 'report_label'):
            self.fields[name].help_text = ''
        link_row_inputs(self)

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if not self.instance.pk and TaxLocation.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError('A tax location with this name already exists.')
        return name


class CategoryForm(forms.ModelForm):
    default_rate = TaxRateField(label='Default rate (%)', required=False, **RATE)

    class Meta:
        model = TaxCategory
        fields = ['name', 'taxable', 'default_rate', 'sort']

    def __init__(self, *args, in_use=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['default_rate'].help_text = ''
        if self.instance.pk:
            self.fields['name'].disabled = True    # items keep the name as text
            self.fields['taxable'].disabled = in_use
        link_row_inputs(self)

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if not self.instance.pk and TaxCategory.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError('A tax category with this name already exists.')
        return name


class BusinessForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    remove_logo = forms.BooleanField(required=False, label='Remove logo')
    MAX_LOGO = 5 * 1024 * 1024
    closing_time = forms.TimeField(required=False, widget=forms.TimeInput(format='%H:%M', attrs={'type': 'time'}),
                                   help_text='Local time after which today can be exported. Blank keeps the saved time.')

    class Meta:
        model = Business
        fields = ['name', 'legal_name', 'short_code', 'address_line1', 'address_line2', 'city', 'region', 'postal_code', 'country',
                  'phone', 'fax', 'email', 'website', 'logo', 'tagline', 'receipt_header_lines', 'return_policy', 'receipt_footer',
                  'order_return_policy', 'order_address_lines', 'receipt_copies', 'order_copies_default', 'label_url', 'serial_prefix',
                  'exchange_outbox', 'closing_time', 'exchange_endpoint']
        widgets = {name: forms.Textarea(attrs={'rows': rows}) for name, rows in
                   (('receipt_header_lines', 4), ('return_policy', 5), ('order_return_policy', 5), ('order_address_lines', 3))}
        widgets['logo'] = forms.FileInput(attrs={'accept': 'image/png,image/jpeg'})
        widgets['closing_time'] = forms.TimeInput(format='%H:%M', attrs={'type': 'time'})

    def clean_exchange_outbox(self):
        from pathlib import Path
        value = self.cleaned_data['exchange_outbox'].strip()
        if value and not Path(value).is_absolute():
            raise forms.ValidationError('Use an absolute directory path.')
        return value

    def clean_closing_time(self):
        return self.cleaned_data.get('closing_time') or self.instance.closing_time

    def clean_exchange_endpoint(self):
        from reports.exchange_sync import validate_endpoint
        value = self.cleaned_data['exchange_endpoint'].strip()
        try:
            validate_endpoint(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return value

    def clean_logo(self):
        logo = self.cleaned_data.get('logo')
        if logo and getattr(logo, 'size', 0) > self.MAX_LOGO:
            raise forms.ValidationError('Choose a logo file under 5 MB.')
        if logo and hasattr(logo, 'content_type') and logo.content_type not in ('image/png', 'image/jpeg'):
            raise forms.ValidationError('Choose a PNG or JPEG file.')
        return logo

    def clean_label_url(self):
        url = self.cleaned_data['label_url'].strip()
        if url and not url.startswith(('https://', 'http://')):
            raise forms.ValidationError('Enter a web address starting with https://, or leave it blank.')
        return url
