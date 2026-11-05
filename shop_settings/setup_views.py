# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-run setup wizard at /setup/<step>/ (steps in setup.STEPS), one screen per step with Back and Next.

Who may use it: while no admin login exists, anyone who can reach the shop may fill in the business, the sales tax and
the first admin (that is how a new install starts). Once an admin exists, every step needs that admin signed in, so a
half-finished setup can't be taken over; creating the admin in step 3 signs it in. After setup, "Run setup again"
(admin only) re-opens the same screens filled in with the current settings.
"""
from functools import wraps

from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from accounts.admin_mode import is_admin
from sales.models import Printer
from sales.printing import PrintError, print_test_page
from . import setup
from .config import business
from .models import Business
from .setup_forms import (TAX_PRESETS, CashierFormSet, SetupAdminForm, SetupBusinessForm, SetupLiveForm, SetupPrinterForm,
                          SetupTaxForm, business_initial, tax_initial)

SLUGS = [slug for slug, _title in setup.STEPS]
CREDENTIAL_FIELDS = ('password1', 'password2', 'register_password1', 'register_password2',
                     *(f'cashier-{n}-pin' for n in range(20)))


def setup_step(slug, needs_admin=False):
    """Access rule for a step (see the module docstring); `needs_admin` steps come after the admin exists."""
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if setup.admin_exists():
                if not is_admin(request):
                    return redirect(f'/login/?next=/setup/{slug}/')
            elif setup.setup_done():
                return render(request, 'setup/no-admin.html', _context(slug), status=403)
            elif needs_admin:
                return redirect('/setup/admin/')
            return view(request, *args, **kwargs)
        return never_cache(require_http_methods(['GET', 'HEAD', 'POST'])(wrapped))
    return decorate


def _context(slug, **extra):
    index = SLUGS.index(slug)
    return dict(title='Set Up Your Shop', step=slug, number=index + 1, total=len(SLUGS), steps=setup.STEPS,
                back=f'/setup/{SLUGS[index - 1]}/' if index else '', next=f'/setup/{SLUGS[index + 1]}/' if index + 1 < len(SLUGS) else '',
                rerun=setup.setup_done(), **extra)


def _next(slug):
    return redirect(f'/setup/{SLUGS[SLUGS.index(slug) + 1]}/')


def start(request):
    return redirect('/setup/business/')


@setup_step('business')
def business_step(request):
    business()   # the row exists (created from the seed on first use)
    row = Business.objects.get()
    form = SetupBusinessForm(instance=row, initial=business_initial(row))
    if request.method == 'POST':
        form = SetupBusinessForm(request.POST, request.FILES, instance=row)
        if form.is_valid():
            setup.save_business(form)
            return _next('business')
    return render(request, 'setup/business.html', _context('business', form=form, row=row), status=400 if request.method == 'POST' else 200)


@setup_step('tax')
def tax_step(request):
    form = SetupTaxForm(initial=tax_initial())
    status = 200
    if request.method == 'POST':
        preset = TAX_PRESETS.get(request.POST.get('preset', ''))
        if preset:
            # A preset button fills in the tax year and frequency and shows the form again; nothing is saved yet.
            typed = {name: request.POST.get(name, '') for name in ('default_rate', 'location_name')}
            form = SetupTaxForm(initial={**typed, 'charge_tax': 'charge_tax' in request.POST, **preset[1]})
        else:
            form = SetupTaxForm(request.POST)
            if form.is_valid():
                setup.save_tax(form.cleaned_data)
                return _next('tax')
            status = 400
    presets = [(key, label) for key, (label, _values) in TAX_PRESETS.items()]
    return render(request, 'setup/tax.html', _context('tax', form=form, presets=presets), status=status)


@setup_step('admin')
@sensitive_post_parameters(*CREDENTIAL_FIELDS)
@sensitive_variables()
def admin_step(request):
    if setup.admin_exists():
        return render(request, 'setup/admin.html', _context('admin', existing=True))
    form, cashiers = SetupAdminForm(), CashierFormSet(prefix='cashier')
    if request.method == 'POST':
        form, cashiers = SetupAdminForm(request.POST), CashierFormSet(request.POST, prefix='cashier')
        if form.is_valid() and cashiers.is_valid():
            admin = setup.create_logins(form, cashiers)
            login(request, admin, backend='django.contrib.auth.backends.ModelBackend')
            return _next('admin')
    return render(request, 'setup/admin.html', _context('admin', form=form, cashiers=cashiers),
                  status=400 if request.method == 'POST' else 200)


@setup_step('printer', needs_admin=True)
def printer_step(request):
    instance = setup.receipt_printer()
    form = SetupPrinterForm(instance=instance, initial={'port': instance.port or 9100})
    message, error, status = '', '', 200
    if request.method == 'POST':
        form = SetupPrinterForm(request.POST, instance=instance)
        action = request.POST.get('action', 'save')
        if form.is_valid():
            if action == 'test':
                if not form.cleaned_data['connected']:
                    error = 'Tick “I have a printer connected” before sending a test page.'
                else:
                    data = form.cleaned_data
                    try:
                        print_test_page(Printer(name=data['name'], host=data['host'], port=data['port'], kind='receipt', active=True))
                        message = f'Test page sent to {data["name"]}. If nothing printed, check the address and that the printer is on.'
                    except PrintError as exc:
                        error = str(exc)
            else:
                setup.save_printer(form)
                return _next('printer')
        status = 400 if (error or form.errors) else 200
    return render(request, 'setup/printer.html', _context('printer', form=form, message=message, error=error), status=status)


@setup_step('demo', needs_admin=True)
def demo_step(request):
    message, error, status = '', setup.catalog_in_use() or '', 200
    if request.method == 'POST':
        try:
            counts = setup.load_demo(with_history=request.POST.get('with_history') == 'on')
            message = 'Demo data loaded: {departments} departments, {items} items, {customers} customers and {vendors} vendors.'.format(**counts)
            error = ''
        except setup.DemoRefused as exc:
            error, status = str(exc), 400
    return render(request, 'setup/demo.html', _context('demo', message=message, error=error,
                                                       loaded=bool(message)), status=status)


@setup_step('live', needs_admin=True)
def live_step(request):
    form = SetupLiveForm(initial={'live': business().live})
    if request.method == 'POST':
        form = SetupLiveForm(request.POST)
        if form.is_valid():
            setup.finish(form.cleaned_data['live'])
            return redirect('/')
    return render(request, 'setup/live.html', _context('live', form=form))
