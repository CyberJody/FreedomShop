# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal
from django import forms
from django.db import models, transaction
from django.forms import formset_factory, inlineformset_factory, BaseInlineFormSet
from config.tax import coherent_tax
from sales.models import Item, quantity_text
from .models import Vendor, Department, ItemVendor


class WholeNumberField(forms.IntegerField):
    """Quantity / count input: spinner steps by 1, fractions rejected with a plain message, never rounded.
    Accepts '5' and '5.000' (Django's IntegerField strips a zero fraction); shows stored Decimals without '.000'."""
    default_error_messages = {'invalid': 'Enter a whole number.'}

    def __init__(self, *, label='Quantity', max_value=999999999, **kwargs):
        super().__init__(label=label, max_value=max_value, step_size=1, **kwargs)  # step_size renders step="1"
        self.widget.attrs.setdefault('inputmode', 'numeric')

    def prepare_value(self, value):
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else quantity_text(value)
        return value


class VendorForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    class Meta:
        model = Vendor
        exclude = []
        widgets = {'street': forms.Textarea(attrs={'rows':3})}


class DepartmentForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from shop_settings.models import TaxCategory
        field = self.fields['tax_category']
        field.to_field_name = 'name'   # posts the category name, as the old text field did
        choices = models.Q(active=True)
        if self.instance.pk:
            choices |= models.Q(pk=self.instance.tax_category_id)
        field.queryset = TaxCategory.objects.filter(choices)
        field.label = 'Tax category'
        field.help_text = 'Categories are set up in Tax Settings.'
        self.fields['tax_rate'].required = False
        if self.instance.tax_category_id:
            self.initial['tax_category'] = self.instance.tax_category.name

    def clean(self):
        data = super().clean()
        if data.get('tax_rate') is not None and not 0 <= data['tax_rate'] <= 100:
            self.add_error('tax_rate', 'Enter a tax rate from 0 to 100.')
        if 'tax_rate' in data:
            category = data.get('tax_category')
            name, data['tax_rate'] = coherent_tax(category.name if category else '', data['tax_rate'])
            if category is None or name != category.name:
                from shop_settings.models import TaxCategory
                data['tax_category'] = TaxCategory.objects.get(name=name)
        return data

    version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    class Meta:
        model = Department
        exclude = []


class TaxRateField(forms.DecimalField):
    """Tax rate (%) input: shows stored rates without trailing zeros (7.000 -> 7); still accepts up to 3 places (8.875)."""
    def prepare_value(self, value):
        return quantity_text(value) if isinstance(value, Decimal) else value


class ItemForm(forms.ModelForm):
    adjustment_note = forms.CharField(max_length=500, required=False, label='Reason for stock/cost change')

    name = forms.CharField(max_length=200, label='Item Name')
    description = forms.CharField(max_length=1000, required=False, label='Item Description', widget=forms.Textarea(attrs={'rows': 3}))
    photo_upload = forms.ImageField(required=False, label='Choose picture', help_text='JPEG or PNG, up to 2 MB.')
    remove_photo = forms.BooleanField(required=False, label='Remove picture')
    on_hand = WholeNumberField(required=False, min_value=-999999999, label='On-Hand Qty', help_text='Stock on hand; a change here is recorded as an adjustment with your reason. A new item left blank starts at 0.')
    reorder_point = WholeNumberField(required=False, min_value=0, label='Reorder point')
    tax_rate = TaxRateField(max_digits=6, decimal_places=3, min_value=0, max_value=100, required=False, label='Tax rate (%)', help_text='A non-taxable category uses 0%. Blank uses the department or the shop default rate (Tax Settings); enter a rate to override.')
    order_cost = forms.DecimalField(max_digits=16, decimal_places=2, min_value=0, required=False, label='Order Cost')
    average_cost = forms.DecimalField(max_digits=16, decimal_places=2, min_value=0, required=False, label='Avg. Unit Cost')
    price = forms.DecimalField(max_digits=16, decimal_places=2, min_value=0, label='Reg Price', required=False, widget=forms.NumberInput(attrs={'step': '0.01'}))
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Item
        fields = ['name', 'sku', 'item_type', 'description', 'primary_vendor', 'department',
                  'size', 'wood', 'style', 'price', 'price_2', 'price_3', 'price_4', 'price_5',
                  'order_cost', 'on_hand', 'average_cost', 'tax_category', 'tax_rate', 'upc', 'alu',
                  'quick_pick_group', 'reorder_point', 'unit_of_measure', 'manufacturer',
                  'location', 'oil_su', 'reverse_notavail', 'pull', 'finish', 'comments',
                  'print_tags', 'unorderable', 'use_serial_numbers', 'earn_commission', 'earn_rewards',
                  'shipping_weight', 'shipping_height', 'shipping_length', 'shipping_width',
                  'shipping_weight_unit', 'shipping_dimension_unit', 'version']  # active = Items Trash
        labels = {'sku': 'Item No.', 'item_type': 'Type', 'description': 'Item Description',
                  'primary_vendor': 'Vendor', 'wood': 'Wood', 'style': 'Style group',
                  'tax_category': 'Tax', 'upc': 'UPC', 'alu': 'ALU', 'location': 'LOC',
                  'oil_su': 'OIL/S.U.', 'reverse_notavail': 'SEE REVERSE/NOTAVAIL',
                  'pull': 'PULL', 'finish': 'FIN/UNF', 'use_serial_numbers': 'Use Serial #',
                  'shipping_weight_unit': 'Weight unit', 'shipping_dimension_unit': 'Dimension unit',
                  'price_2': 'Price 2', 'price_3': 'Price 3', 'price_4': 'Price 4', 'price_5': 'Price 5'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3}),
                   'comments': forms.Textarea(attrs={'rows': 8, 'maxlength': 5000})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sku'].required = False
        self.fields['sku'].disabled = True
        self.fields['sku'].help_text = 'Assigned automatically when saved.' if not self.instance.pk else 'Internal item number.'
        self.fields['sku'].widget.attrs['placeholder'] = 'Automatic'
        for name in ['reorder_point', 'price_2', 'price_3', 'price_4', 'price_5',
                     'shipping_weight', 'shipping_height', 'shipping_length', 'shipping_width']:
            self.fields[name].min_value = 0
            self.fields[name].widget.attrs['min'] = 0
        self.fields['comments'].max_length = 5000
        from shop_settings.config import tax
        names = [(c.name, c.name if c.taxable else f'{c.name} (no tax)') for c in tax().categories if c.active]
        current = self.instance.tax_category
        self.fields['tax_category'].widget = forms.Select(choices=names + ([(current, current)] if current and current not in dict(names) else []))
        self.fields['unit_of_measure'].help_text = 'Base unit for all quantities; pack conversions are not available.'
        self.fields['use_serial_numbers'].help_text = 'Manufacturer serial tracking is not available; PO piece labels are separate.'
        self.fields['earn_commission'].help_text = 'Eligibility setting only; commission calculation is not available.'
        self.fields['earn_rewards'].help_text = 'Eligibility setting only; reward calculation is not available.'
        self.fields['unorderable'].help_text = 'Blocks purchase orders and receiving until cleared.'

    def clean_photo_upload(self):
        upload = self.cleaned_data.get('photo_upload')
        if not upload:
            return None
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError('Choose a picture no larger than 2 MB.')
        from io import BytesIO
        from PIL import Image
        try:
            upload.seek(0)
            with Image.open(upload) as photo:
                if photo.format not in {'JPEG', 'PNG'} or photo.width * photo.height > 16_000_000:
                    raise ValueError
                photo.load()
                photo.thumbnail((1200, 1200))
                clean = Image.new('RGB', photo.size, 'white')
                if photo.mode == 'RGBA':
                    clean.paste(photo, mask=photo.getchannel('A'))
                else:
                    clean.paste(photo.convert('RGB'))
                output = BytesIO()
                clean.save(output, format='JPEG', quality=85)
            return output.getvalue()
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            raise forms.ValidationError('Choose a valid JPEG or PNG under 16 megapixels.') from exc

    def clean(self):
        data = super().clean()
        if 'tax_rate' in data:
            department=data.get('department')
            category=data.get('tax_category') or (department.category_name if department else '')
            default=department.tax_rate if department else None
            data['tax_category'],data['tax_rate']=coherent_tax(category,data['tax_rate'],default)
        for name in ['reorder_point', 'price_2', 'price_3', 'price_4', 'price_5',
                     'shipping_weight', 'shipping_height', 'shipping_length', 'shipping_width']:
            if data.get(name) is not None and data[name] < 0:
                self.add_error(name, 'Enter zero or a positive value.')
        if data.get('shipping_weight') not in (None, 0) and not data.get('shipping_weight_unit'):
            self.add_error('shipping_weight_unit', 'Select the weight unit.')
        if any(data.get(name) not in (None, 0) for name in ['shipping_height', 'shipping_length', 'shipping_width']) and not data.get('shipping_dimension_unit'):
            self.add_error('shipping_dimension_unit', 'Select the dimension unit.')
        if not data.get('description') and data.get('name'):
            data['description'] = data['name']
        if not self.instance.pk and 'on_hand' in data and data['on_hand'] is None:
            # like QB, a new item starts with 0 on hand, so it can be received at once. Only imported QB items can
            # have an unknown ("Unverified") count; Avg. Unit Cost left blank is set by the first receiving.
            data['on_hand'] = Decimal('0')
        return data

    def save(self, commit=True):
        item = super().save(commit=False)
        if self.cleaned_data.get('remove_photo'):
            item.photo = None
        if self.cleaned_data.get('photo_upload'):
            item.photo = self.cleaned_data['photo_upload']
        if commit:
            with transaction.atomic():
                if not item.pk:
                    from .item_numbers import next_item_number
                    item.sku = next_item_number()
                item.save()
        return item


class ItemVendorForm(forms.ModelForm):
    order_cost = forms.DecimalField(max_digits=16, decimal_places=2, min_value=0, required=False)
    class Meta:
        model = ItemVendor
        fields = ['vendor', 'order_cost', 'upc', 'alu']
        labels = {'upc': 'UPC', 'alu': 'ALU'}


class AlternateVendorFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        for form in self.forms:
            data = getattr(form, 'cleaned_data', {})
            if data and not data.get('DELETE') and data.get('vendor') == self.instance.primary_vendor:
                raise forms.ValidationError('The primary vendor should not also be an alternate vendor.')


ItemVendorFormSet = inlineformset_factory(Item, ItemVendor, form=ItemVendorForm,
    formset=AlternateVendorFormSet, extra=4, max_num=4, validate_max=True, absolute_max=4, can_delete=True)


class HeaderForm(forms.Form):
    token = forms.UUIDField(widget=forms.HiddenInput)
    version = forms.IntegerField(widget=forms.HiddenInput, initial=0, min_value=0)
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.all())
    status = forms.ChoiceField(choices=[('open','Open'),('pending','Pending'),('closed','Closed')],required=False)
    notes = forms.CharField(max_length=5000, required=False, widget=forms.Textarea(attrs={'rows':3}), label='Instructions')
    terms = forms.CharField(max_length=200, required=False)
    ship_date = forms.DateField(required=False, label='Preferred Pickup Date', widget=forms.DateInput(attrs={'type':'date'}))
    alternate_pickup_date = forms.DateField(required=False, label='Alternate Pickup Date', widget=forms.DateInput(attrs={'type':'date'}))
    payment_due = forms.DateField(required=False, widget=forms.DateInput(attrs={'type':'date'}))
    invoice_number = forms.CharField(max_length=100, required=False, label='Vendor Invoice #')


class LineForm(forms.Form):
    line_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    item = forms.ModelChoiceField(queryset=Item.objects.all(),widget=forms.HiddenInput)
    item_name = forms.CharField(required=False,widget=forms.TextInput(attrs={'class':'item-search','placeholder':'Scan UPC or ALU, or find item by name or number','autocomplete':'off','aria-label':'Scan barcode or find inventory item'}))
    item_description = forms.CharField(required=False,widget=forms.Textarea(attrs={'rows':2,'readonly':True,'aria-label':'Item description','class':'purchase-description'}))
    quantity = WholeNumberField(min_value=0,initial=1,label='Qty',widget=forms.NumberInput(attrs={'class':'quantity','aria-label':'Quantity'}),error_messages={'invalid':'Quantity must be a whole number.'})  # 0 = skip the line (whole numbers only)
    cost = forms.DecimalField(max_digits=12,decimal_places=2,min_value=0,label='Unit Cost',widget=forms.NumberInput(attrs={'step':'0.01','class':'cost','aria-label':'Unit cost'}))
    source_line = forms.IntegerField(required=False,widget=forms.HiddenInput)
    sales_line = forms.IntegerField(required=False,widget=forms.HiddenInput)
    def clean(self):
        cleaned=super().clean()
        if cleaned.get('quantity')==0:
            cleaned['DELETE']=True
        return cleaned


LineSet = formset_factory(LineForm,extra=0,can_delete=True,max_num=100,validate_max=True,absolute_max=100)


class POLineForm(LineForm):
    """on a purchase order the line's description is editable, so the builder can be given extra
    details (or have extraneous text removed). Saved on the PO line only; the Item List and the sales order keep
    their own text. Receiving vouchers keep the read-only box and carry the PO line's text."""
    item_description = forms.CharField(required=False, max_length=1000, strip=True, widget=forms.Textarea(attrs={
        'rows': 2, 'maxlength': 1000, 'aria-label': 'Item description (prints on this PO)', 'class': 'purchase-description editable',
        'title': 'Edits print on this PO only; the Item List and the sales order keep their own text.'}))


POLineSet = formset_factory(POLineForm,extra=0,can_delete=True,max_num=100,validate_max=True,absolute_max=100)
