# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sales tax setup (one TaxSettings row, the shop's tax locations and tax categories).

Tax math itself (per rate on the exact taxable amount, half-up once to cents) lives in sales.models.tax_amount and does
not change here; these rows say which rates, labels, report sections and filing periods the shop uses.
"""
import uuid
from datetime import time

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

MONTHS = [(n, name) for n, name in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                                              'September', 'October', 'November', 'December'], start=1)]
RATE = dict(max_digits=6, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(100)])


class TaxLocation(models.Model):
    """A place the shop collects tax for. Document.tax_location / Customer.tax_location keep the name as text, so a
    location in use can be retired (hidden from new choices) but never deleted or renamed."""
    name = models.CharField(max_length=100, unique=True)   # same width as Document.tax_location
    rate = models.DecimalField(default=0, **RATE)
    label = models.CharField(max_length=100, blank=True, help_text='On receipts before the rate, e.g. "Main Street 7 % Tax:". Blank = the name.')
    report_label = models.CharField(max_length=150, blank=True, help_text='Sales Tax report section title. Blank = the name.')
    is_exempt = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    sort = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['is_exempt', 'sort', 'name']
        constraints = [models.CheckConstraint(condition=models.Q(is_exempt=False) | models.Q(rate=0), name='exempt_location_rate_zero')]

    def __str__(self):
        return self.name

    @property
    def receipt_label(self):
        return self.label or self.name

    @property
    def section_label(self):
        return self.report_label or self.name


class TaxCategoryManager(models.Manager):
    def get_by_natural_key(self, name):
        """Fixtures name a category as ["Taxable"] / ["Non-taxable"]: the shop's own category of that kind (or the
        same name) is used, so the demo data fits whatever categories the shop has (config.category_named)."""
        from .config import category_named
        return category_named(name)


class TaxCategory(models.Model):
    """What a department's items are: taxable or not. Item.tax_category keeps the name as text."""
    name = models.CharField(max_length=100, unique=True)
    taxable = models.BooleanField(default=True)
    default_rate = models.DecimalField(null=True, blank=True, help_text='Blank = the shop default rate.', **RATE)
    active = models.BooleanField(default=True)
    sort = models.PositiveIntegerField(default=0)

    objects = TaxCategoryManager()

    class Meta:
        ordering = ['-taxable', 'sort', 'name']
        verbose_name_plural = 'tax categories'

    def __str__(self):
        return self.name

    def natural_key(self):
        return (self.name,)


class TaxSettings(models.Model):
    """The one row of general tax settings."""
    BASIS = [('cash', 'Cash (tax counts when money is received)'), ('accrual', 'Accrual (tax counts when sold)')]
    FREQUENCY = [('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annually', 'Annually')]
    ROUNDING = [('half_up', 'Half-up to the cent, per rate (QuickBooks POS compatible)')]

    charge_tax = models.BooleanField(default=True)
    default_location = models.ForeignKey(TaxLocation, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    default_rate = models.DecimalField(default=0, **RATE)
    rounding = models.CharField(max_length=20, choices=ROUNDING, default='half_up')
    basis = models.CharField(max_length=10, choices=BASIS, default='cash')
    filing_frequency = models.CharField(max_length=10, choices=FREQUENCY, default='quarterly')
    tax_year_start_month = models.PositiveSmallIntegerField(choices=MONTHS, default=1, null=True, validators=[MinValueValidator(1), MaxValueValidator(12)])
    estimate_rule = models.CharField(max_length=300, blank=True, help_text='Wording for the Sales Tax report note on estimated payments. Blank = worked out from the default location.')
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name_plural = 'tax settings'

    def __str__(self):
        return 'Tax settings'


class Business(models.Model):
    """The one row of business details printed on receipts, orders, tags, POs and reports and shown in title bars."""
    exchange_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    exchange_outbox = models.CharField(max_length=500, blank=True,
                                       help_text='Absolute directory for daily exchange files and FreedomBooks acknowledgements.')
    closing_time = models.TimeField(default=time(23, 59), help_text='Local time after which today can be exported.')
    exchange_endpoint = models.URLField(max_length=500, blank=True,
                                        help_text='Draft command receiver URL (HTTPS). Blank disables live push.')
    # TODO: add an encrypted exchange_token field only after cryptography is in requirements.lock.
    # Never accept or persist a plaintext credential in the meantime.
    name = models.CharField(max_length=120, default='My Shop', help_text='Trade name on screens, tags and report headings.')
    legal_name = models.CharField(max_length=200, blank=True, help_text='e.g. "My Shop, Inc." — first line of the receipt and PO header. Blank = the name.')
    short_code = models.CharField(max_length=10, blank=True, help_text='Store code on sales orders ("Store: ...").')
    serial_prefix = models.CharField(max_length=4, default='FS',
                                     validators=[RegexValidator(r'\A[A-Z0-9]{2,4}\Z', 'Use 2–4 uppercase letters or digits.')],
                                     help_text='Prefix for new piece labels. Existing codes keep their prefix.')
    address_line1 = models.CharField('Address', max_length=200, blank=True)
    address_line2 = models.CharField('Address line 2', max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField('State / region', max_length=100, blank=True)
    postal_code = models.CharField('ZIP / postal code', max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    fax = models.CharField(max_length=50, blank=True)
    email = models.EmailField(max_length=200, blank=True)
    website = models.CharField(max_length=200, blank=True)
    logo = models.ImageField(upload_to='branding/', blank=True, help_text='PNG or JPEG, black on white; printed in black and white.')
    tagline = models.CharField(max_length=100, blank=True, help_text='Printed under the name when there is no logo.')
    receipt_header_lines = models.TextField(blank=True, help_text='Receipt header, one line each. Blank = legal name, address and phone.')
    receipt_footer = models.CharField(max_length=200, blank=True, default='Thank you for your order!', help_text='Thank-you line on sales orders.')
    return_policy = models.TextField(blank=True, help_text='Printed at the bottom of every receipt.')
    order_return_policy = models.TextField('Sales order policy', blank=True, help_text='Printed on sales orders. Blank = the return policy.')
    order_address_lines = models.TextField('Sales order address lines', blank=True, help_text='Bottom of the sales order, one line each. Blank = legal name, address and phone.')
    label_url = models.CharField('Item label web address', max_length=300, blank=True,
                                 help_text='QR code address on PO item labels, with {code} where the serial goes. Blank = the server setting.')
    receipt_copies = models.PositiveSmallIntegerField(default=2, validators=[MinValueValidator(1), MaxValueValidator(5)],
                                                      help_text='Copies of a completed receipt on the receipt printer.')
    order_copies_default = models.PositiveSmallIntegerField('Sales order copies', default=3, validators=[MinValueValidator(1), MaxValueValidator(9)],
                                                            help_text='Starting copies for a new Letter printer.')
    time_zone = models.CharField(max_length=64, blank=True,
                                 help_text='e.g. America/New_York. Blank = the server setting. Takes effect when the app restarts.')
    currency = models.CharField(max_length=3, default='USD', help_text='ISO 4217 code, e.g. USD. Recorded for your books; amounts show with $.')
    # Testing marks print while this is off; PRINT_TESTING_MARKS = False in the site settings turns them off regardless.
    live = models.BooleanField('Live shop', default=False, help_text='On: printouts no longer carry the testing bars.')
    # Set at the end of the first-run setup (the wizard at /setup/ or `manage.py setup`); until then every page goes to /setup/.
    setup_complete = models.BooleanField(default=False, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name_plural = 'business'

    def __str__(self):
        return self.name

    @property
    def city_line(self):
        place = ', '.join(part for part in (self.city, ' '.join(p for p in (self.region, self.postal_code) if p)) if part)
        return place

    def address_lines(self):
        """Street lines and the city line (PO header)."""
        return [line for line in (self.address_line1, self.address_line2, self.city_line) if line]

    def receipt_lines(self):
        """The receipt header under the logo: the first line bold. Explicit lines, else legal name, address, phone."""
        explicit = [line.strip() for line in self.receipt_header_lines.splitlines() if line.strip()]
        return explicit or [self.legal_name or self.name, *self.address_lines(), *([self.phone] if self.phone else [])]

    def order_lines(self):
        """The block at the bottom of a sales order. Explicit lines, else legal name, "street - city line", phone."""
        explicit = [line.strip() for line in self.order_address_lines.splitlines() if line.strip()]
        street = ' - '.join(part for part in (', '.join(p for p in (self.address_line1, self.address_line2) if p), self.city_line) if part)
        return explicit or [line for line in (self.legal_name or self.name, street, self.phone) if line]

    @property
    def order_policy(self):
        return self.order_return_policy or self.return_policy


class Backup(models.Model):
    stamp = models.CharField(primary_key=True, max_length=15)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=12, choices=[(v, v.title()) for v in ('manual', 'scheduled', 'safety')])
    size = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=10, default='failed', choices=[('verified', 'Verified'), ('failed', 'Failed')])
    error = models.TextField(blank=True)
    path = models.CharField(max_length=1000)
    download_sha256 = models.CharField(max_length=64, blank=True)
    restore_report = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-stamp']


class BackupSchedule(models.Model):
    # Singleton; the scheduler also holds a filesystem lock across the due check and run.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    time = models.TimeField(default=time(2, 0))
    retention = models.PositiveSmallIntegerField(default=14, validators=[MinValueValidator(1), MaxValueValidator(3650)])
    last_attempt_day = models.DateField(null=True, blank=True)
    last_run = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)


class RestoreLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup = models.ForeignKey(Backup, on_delete=models.PROTECT, related_name='restore_logs')
    created_at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=12)
    status = models.CharField(max_length=20)
    step = models.CharField(max_length=80)
    safety_stamp = models.CharField(max_length=15, blank=True)
    report = models.JSONField(default=dict)
    error = models.TextField(blank=True)

REGISTER_RIGHTS = ('edit_prices', 'edit_costs', 'see_costs', 'adjust_stock', 'edit_items',
                   'edit_vendors', 'edit_departments', 'manage_charge_accounts', 'receipt_discounts')


class RegisterRules(models.Model):
    """One saved policy row. Admin rights are unconditional; PIN identities use their login's rights."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    require_customer = models.BooleanField(default=False, verbose_name='Require a customer on every sale')
    edit_prices = models.BooleanField(default=False)
    edit_costs = models.BooleanField(default=False)
    see_costs = models.BooleanField(default=False)
    adjust_stock = models.BooleanField(default=False)
    edit_items = models.BooleanField(default=False)
    edit_vendors = models.BooleanField(default=False)
    edit_departments = models.BooleanField(default=False)
    manage_charge_accounts = models.BooleanField(default=False)
    receipt_discounts = models.BooleanField(default=True)
    discount_override_enabled = models.BooleanField(default=False)
    discount_override_percent = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    require_discount_reason = models.BooleanField(default=False, verbose_name='Require a discount reason')
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(pk=1), name='register_rules_singleton'),
                       models.CheckConstraint(condition=models.Q(discount_override_percent__lte=100), name='register_override_percent_range')]


class RegisterOverrideCache(models.Model):
    """Shared cache schema for approval failure counters and their short serialization locks."""
    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.TextField()
    expires = models.DateTimeField(db_index=True)
