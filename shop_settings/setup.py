# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run setup: what the wizard (/setup/, setup_views.py) and `manage.py setup` write, step by step.

Every step changes rows the Settings screens edit later (Business, Tax Settings, Printers, logins); nothing writes a
Python settings file. Credentials are hashed on the way in and never logged, echoed or returned.
"""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from accounts.models import Cashier
from accounts.roles import ADMIN_GROUP, REGISTER_GROUP, group
from .config import business, ensure_configured, tax
from .models import Business, TaxLocation, TaxSettings

DEMO_FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'demo_shop.json'
# The wizard's steps in order: (slug, title). /setup/<slug>/.
STEPS = [('business', 'Business'), ('tax', 'Sales tax'), ('admin', 'Admin login'), ('printer', 'Receipt printer'),
         ('demo', 'Demo data'), ('live', 'Live shop')]


class DemoRefused(Exception):
    pass


def setup_done():
    return business().setup_complete


def admin_exists():
    return get_user_model().objects.filter(is_active=True, groups__name=ADMIN_GROUP).exists()


def save_business(form):
    """Step 1: the Business Settings fields the wizard asks for (a SetupBusinessForm that is valid)."""
    with transaction.atomic():
        row = Business.objects.select_for_update().get(pk=form.instance.pk)
        old_logo = row.logo.name
        saved = form.save(commit=False)
        if form.cleaned_data.get('remove_logo') and not form.files.get('logo'):
            saved.logo = ''
        saved.version = row.version + 1
        saved.save()
        if old_logo and old_logo != saved.logo.name:
            transaction.on_commit(lambda: default_storage.delete(old_logo))
    return saved


def save_tax(data):
    """Step 2: Tax Settings and the default (taxable) location from a SetupTaxForm's cleaned data. The exempt location
    and the categories come from the seed (ensure_configured) when missing; an existing location of the same name is
    reused, and the seed's placeholder location is removed once nothing uses it."""
    from .views import location_in_use
    ensure_configured()
    with transaction.atomic():
        row = TaxSettings.objects.select_for_update(of=('self',)).select_related('default_location').get()
        old = row.default_location
        rate = data['default_rate'] if data['charge_tax'] else 0
        location = TaxLocation.objects.filter(name__iexact=data['location_name'], is_exempt=False).first() \
            or TaxLocation(name=data['location_name'], sort=0)
        location.rate, location.active = rate, True
        location.save()
        row.charge_tax, row.default_rate, row.default_location = data['charge_tax'], rate, location
        row.tax_year_start_month, row.filing_frequency = data['tax_year_start_month'], data['filing_frequency']
        row.version += 1
        row.save()
        if old is not None and old.pk != location.pk and not location_in_use(old):
            old.delete()
    return tax()


@sensitive_variables()
def create_logins(admin_form, cashier_formset=None):
    """Step 3: the first admin (Admin group), the optional shared register login (Register group) and PIN cashiers
    (accounts.models.Cashier, PIN hashed as accounts/cashiers.py checks it). Returns the admin user."""
    User = get_user_model()
    data = admin_form.cleaned_data
    with transaction.atomic():
        admin = User(username=data['username'])
        admin.set_password(data['password1'])
        admin.save()
        admin.groups.add(group(ADMIN_GROUP))
        if data.get('register_username'):
            register = User(username=data['register_username'])
            register.set_password(data['register_password1'])
            register.save()
            register.groups.add(group(REGISTER_GROUP))
        for name, pin in (cashier_formset.cashiers() if cashier_formset is not None else []):
            Cashier.objects.create(name=name, pin_hash=make_password(pin))
    return admin


def receipt_printer():
    """The printer the wizard edits: the first receipt printer, if any (a new one otherwise)."""
    from sales.models import Printer
    return Printer.objects.filter(kind='receipt').order_by('-active', 'pk').first() or Printer(kind='receipt')


def save_printer(form):
    """Step 4: one receipt printer (name, host, port)."""
    printer = form.save(commit=False)
    printer.kind = 'receipt'
    printer.active = True
    printer.version = (printer.version + 1) if printer.pk else 1
    printer.save()
    return printer


def catalog_in_use():
    """Why demo data can't be loaded (None when the catalog is empty): it only goes into a shop with no customers,
    items, departments or vendors, so it never mixes with real records or reuses their numbers."""
    from customers.models import Customer
    from purchasing.models import Department, Vendor
    from sales.models import Item
    for model, label in ((Customer, 'customers'), (Item, 'items'), (Department, 'departments'), (Vendor, 'vendors')):
        if model.objects.exists():
            return f'Demo data can only go into an empty shop, and this one already has {label}.'
    return None


def load_demo(with_history=False):
    """Step 5: keep the small fixture, or opt into the service-backed history command.

    Both paths retain the preceding wizard settings and refuse a non-empty shop.
    """
    if with_history:
        # Use the same command as an operator; preserve the preceding wizard steps.
        from io import StringIO
        from django.core.management.base import CommandError
        try:
            call_command('load_demo', with_history=True, preserve_settings=True, stdout=StringIO(), verbosity=0)
        except CommandError as exc:
            raise DemoRefused(str(exc)) from exc
        return {'departments': 12, 'items': 250, 'customers': 120, 'vendors': 15}
    from customers.models import Customer
    from purchasing.models import Department, Vendor
    from sales.models import Item
    refusal = catalog_in_use()
    if refusal:
        raise DemoRefused(refusal)
    with transaction.atomic():
        call_command('loaddata', str(DEMO_FIXTURE), exclude=['shop_settings.Business'], verbosity=0)
        rate = tax().line_rate
        for department in Department.objects.select_related('tax_category'):
            department.tax_rate = rate if department.tax_category.taxable else 0
            department.save(update_fields=['tax_rate'])
            Item.objects.filter(department=department).update(tax_category=department.category_name, tax_rate=department.tax_rate)
    return {'departments': Department.objects.count(), 'items': Item.objects.count(), 'customers': Customer.objects.count(),
            'vendors': Vendor.objects.count()}


def finish(live):
    """Step 6: live or testing, and setup is done (the gate opens)."""
    with transaction.atomic():
        row = Business.objects.select_for_update().get(pk=business().pk)
        row.live = bool(live)
        row.setup_complete = True
        row.version += 1
        row.save()
    return row
