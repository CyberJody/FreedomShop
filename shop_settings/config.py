# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read the shop's settings: the tax setup (one frozen snapshot) and the Business row, cached until a settings row is
saved or the next request starts.

The rows are created on first use (and after `migrate` / a test flush) from the seed file named by
settings.SHOP_SETTINGS_SEED — `seeds/default.json` for a new shop. When the database can't be read at all (no tables
yet, or a test that forbids queries) the seed values are used without saving anything.
"""
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, transaction
from .normalization import location_key

SEED_DEFAULT = Path(__file__).resolve().parent / 'seeds' / 'default.json'
# Category names the old free-text field used for "no tax" and "taxed" (QuickBooks POS wording and common variants).
NON_KEYS = {'non', 'nontaxable', 'nontaxablesales', 'notax', 'taxexempt'}
TAXABLE_KEYS = {'', 'tax', 'taxable', 'taxablesales'}
# AssertionError: a test case that forbids database queries (Django raises DatabaseOperationForbidden, a subclass).
UNREADABLE = (DatabaseError, AssertionError)

_cache = {}
_cache_generation = 0


def clear_cache(*args, **kwargs):
    global _cache_generation
    _cache.clear()
    _cache_generation += 1


def _settings_cache():
    """Uncommitted snapshots belong to the innermost atomic block, never the process cache.

    Django discards that block on rollback (including savepoint rollback). A parent block's old snapshot is
    invalidated by the save signal's generation change. Commit clears the local snapshot too; a later read gets
    committed rows. Outside transactions the request-start/save signals still invalidate the process cache.
    """
    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        return _cache
    block = connection.atomic_blocks[-1]
    generation, values = getattr(block, '_shop_settings_cache', (None, None))
    if generation != _cache_generation:
        values = {}
        block._shop_settings_cache = (_cache_generation, values)
        transaction.on_commit(values.clear)
    return values


def seed():
    path = getattr(settings, 'SHOP_SETTINGS_SEED', '') or SEED_DEFAULT
    return json.loads(Path(path).read_text())


def category_key(name):
    return (name or '').strip().casefold().replace('-', '').replace(' ', '')


@dataclass(frozen=True)
class Location:
    name: str
    rate: Decimal
    label: str = ''
    report_label: str = ''
    is_exempt: bool = False
    active: bool = True

    @property
    def receipt_label(self):
        return self.label or self.name

    @property
    def section_label(self):
        return self.report_label or self.name


@dataclass(frozen=True)
class Category:
    name: str
    taxable: bool = True
    default_rate: Decimal = None
    active: bool = True


@dataclass(frozen=True)
class TaxConfig:
    charge_tax: bool
    default_rate: Decimal
    basis: str
    filing_frequency: str
    tax_year_start_month: int
    estimate_rule: str
    rounding: str
    locations: tuple          # every location, retired ones too (old documents keep their names)
    default_location: Location
    exempt_location: Location
    categories: tuple

    @property
    def line_rate(self):
        """The last fallback rate for a sale line (line, customer, item, then this): 0 when the shop charges no tax."""
        return self.default_rate if self.charge_tax else Decimal('0')

    @property
    def line_rate_text(self):
        return f'{self.line_rate.normalize():f}'

    @property
    def estimate_fraction(self):
        """How tax is estimated inside money with no charge behind it: "7/107" for a 7 % default location."""
        rate = self.default_location.rate.normalize()
        return f'{rate:f}/{(100 + rate).normalize():f}'

    @property
    def estimate_note(self):
        """The Sales Tax report's wording for that estimate (Tax Settings' own wording when the shop set one)."""
        return self.estimate_rule or f'exactly {self.estimate_fraction} for {self.default_location.name}, zero for {self.exempt_location.name}'

    def location(self, name):
        key = location_key(name)
        return next((loc for loc in self.locations if location_key(loc.name) == key), None)

    def location_for(self, exempt):
        """The location name a sale or customer gets from its Tax Exempt checkbox."""
        return self.exempt_location.name if exempt else self.default_location.name

    def section_of(self, name, exempt):
        """The report section a stored tax location lands in: its own if it is a known location, else by the exempt flag."""
        loc = self.location(name) if name else None
        return loc.name if loc is not None else self.location_for(exempt)

    @property
    def sections(self):
        """Sales Tax report sections in order: the default location, other active taxable ones, the exempt one last."""
        others = [loc for loc in self.locations if loc.active and not loc.is_exempt and loc != self.default_location]
        return [self.default_location, *others, self.exempt_location]

    @property
    def taxable_category(self):
        return next(c for c in self.categories if c.taxable and c.active)

    @property
    def nontaxable_category(self):
        return next(c for c in self.categories if not c.taxable and c.active)

    @property
    def customer_choices(self):
        rate = f'{self.default_location.rate.normalize():f}'
        return [(self.default_location.name, f'{self.default_location.name} ({rate}%)'), (self.exempt_location.name, self.exempt_location.name)]


def tax():
    """The shop's tax setup (TaxConfig)."""
    cache = _settings_cache()
    if 'tax' not in cache:
        try:
            config = _read_tax()
        except UNREADABLE:
            return _seed_tax()
        cache['tax'] = config
    return cache['tax']


def _location(row):
    return Location(row.name, row.rate, row.label, row.report_label, row.is_exempt, row.active)


def _category(row):
    return Category(row.name, row.taxable, row.default_rate, row.active)


def _read_tax():
    from .models import TaxCategory, TaxLocation, TaxSettings
    row = TaxSettings.objects.select_related('default_location').first()
    if row is None or row.default_location is None or not TaxLocation.objects.filter(is_exempt=True).exists() \
            or not TaxCategory.objects.filter(taxable=True).exists() or not TaxCategory.objects.filter(taxable=False).exists():
        ensure_configured()
        row = TaxSettings.objects.select_related('default_location').get()
    locations = [_location(loc) for loc in TaxLocation.objects.all()]
    exempt = next((loc for loc in locations if loc.is_exempt and loc.active), None) or next(loc for loc in locations if loc.is_exempt)
    categories = tuple(_category(c) for c in TaxCategory.objects.all())
    return TaxConfig(row.charge_tax, row.default_rate, row.basis, row.filing_frequency, row.tax_year_start_month, row.estimate_rule,
                     row.rounding, tuple(locations), _location(row.default_location), exempt, categories)


def _seed_tax():
    data = seed()
    general = data['tax']
    locations = tuple(Location(loc['name'], Decimal(loc.get('rate', '0')), loc.get('label', ''), loc.get('report_label', ''),
                               loc.get('is_exempt', False)) for loc in data['locations'])
    categories = tuple(Category(c['name'], c.get('taxable', True), Decimal(c['default_rate']) if c.get('default_rate') else None)
                       for c in data['categories'])
    default = next(loc for loc in locations if loc.name == general['default_location'])
    exempt = next(loc for loc in locations if loc.is_exempt)
    return TaxConfig(general.get('charge_tax', True), Decimal(general.get('default_rate', '0')), general.get('basis', 'cash'),
                     general.get('filing_frequency', 'quarterly'), general.get('tax_year_start_month', 1), general.get('estimate_rule', ''),
                     'half_up', locations, default, exempt, categories)


BUSINESS_FIELDS = ('name', 'legal_name', 'short_code', 'address_line1', 'address_line2', 'city', 'region', 'postal_code', 'country',
                   'phone', 'fax', 'email', 'website', 'tagline', 'receipt_header_lines', 'receipt_footer', 'return_policy',
                   'order_return_policy', 'order_address_lines', 'label_url', 'serial_prefix', 'receipt_copies', 'order_copies_default')


def _seed_business():
    from .models import Business
    values = seed().get('business', {})
    return Business(**{name: values[name] for name in BUSINESS_FIELDS if name in values})


def business():
    """The shop's Business row (created from the seed on first use; unsaved seed values when the database can't be read)."""
    cache = _settings_cache()
    if 'business' not in cache:
        from .models import Business
        try:
            row = Business.objects.first()
            if row is None:
                row = _seed_business()
                row.save()
        except UNREADABLE:
            return _seed_business()
        cache['business'] = row
    return cache['business']


def report_heading():
    """The first line of every report export (XLSX first row, PDF title and footer): the business name."""
    return business().name


def ensure_configured(using='default'):
    """Fill in whatever part of the tax setup is missing from the seed: categories (at least one taxable and one not),
    locations (at least one exempt), and the settings row with its default location. Never changes existing rows."""
    from .models import TaxCategory, TaxLocation, TaxSettings
    data = seed()
    general = data['tax']
    with transaction.atomic(using=using):
        categories = TaxCategory.objects.using(using)
        if not categories.exists():
            for n, c in enumerate(data['categories']):
                categories.create(name=c['name'], taxable=c.get('taxable', True), default_rate=c.get('default_rate'), sort=n)
        for taxable in (True, False):
            if not categories.filter(taxable=taxable).exists():
                c = next(c for c in data['categories'] if c.get('taxable', True) == taxable)
                categories.get_or_create(name=c['name'], defaults=dict(taxable=taxable, default_rate=c.get('default_rate')))
        locations = TaxLocation.objects.using(using)
        if not locations.exists():
            for n, loc in enumerate(data['locations']):
                locations.create(name=loc['name'], rate=loc.get('rate', '0'), label=loc.get('label', ''), report_label=loc.get('report_label', ''),
                                 is_exempt=loc.get('is_exempt', False), sort=n)
        if not locations.filter(is_exempt=True).exists():
            loc = next(loc for loc in data['locations'] if loc.get('is_exempt'))
            locations.create(name=loc['name'], rate=0, label=loc.get('label', ''), report_label=loc.get('report_label', ''), is_exempt=True, sort=99)
        row = TaxSettings.objects.using(using).first()
        if row is None or row.default_location_id is None:
            default = (locations.filter(name=general['default_location'], is_exempt=False).first()
                       or locations.filter(is_exempt=False, active=True).first())
            if default is None:
                loc = next(loc for loc in data['locations'] if loc['name'] == general['default_location'])
                default = locations.create(name=loc['name'], rate=loc.get('rate', '0'), label=loc.get('label', ''), report_label=loc.get('report_label', ''))
            if row is None:
                TaxSettings.objects.using(using).create(
                    charge_tax=general.get('charge_tax', True), default_location=default, default_rate=general.get('default_rate', '0'),
                    basis=general.get('basis', 'cash'), filing_frequency=general.get('filing_frequency', 'quarterly'),
                    tax_year_start_month=general.get('tax_year_start_month', 1), estimate_rule=general.get('estimate_rule', ''))
            else:
                row.default_location = default
                row.save(update_fields=['default_location'])
        from .models import Business
        if not Business.objects.using(using).exists():
            _seed_business().save(using=using)
    clear_cache()


def category_named(name):
    """The TaxCategory row for a name as the old text field held it: the same name (any case), else "Non"-style names
    -> the first non-taxable category and "Taxable Sales"-style names -> the first taxable one; any other name becomes
    a new taxable category, as free text did before."""
    from .models import TaxCategory
    name = name or ''
    row = TaxCategory.objects.filter(name=name).first() if name else None
    if row is None:
        name = name.strip()
        row = TaxCategory.objects.filter(name__iexact=name).first() if name else None
    if row is not None:
        return row
    key = category_key(name)
    if key in NON_KEYS or key in TAXABLE_KEYS:
        config = tax()
        target = config.nontaxable_category if key in NON_KEYS else config.taxable_category
        return TaxCategory.objects.get(name=target.name)
    return TaxCategory.objects.create(name=name[:100], taxable=True)


def default_tax_category():
    """Department.tax_category default: the first taxable category (None when the table can't be read yet)."""
    from .models import TaxCategory
    try:
        row = TaxCategory.objects.filter(name=tax().taxable_category.name).first()
    except UNREADABLE:
        return None
    return row.pk if row else None


def default_tax_rate():
    """New departments use the active taxable category's rate, then the shop default (0 when tax is off)."""
    config = tax()
    rate = config.taxable_category.default_rate
    return rate if config.charge_tax and rate is not None else config.line_rate


def default_estimate_rate():
    """Snapshot for unexplained account money, stored on each document before rates can change."""
    return tax().default_location.rate


def default_estimate_location():
    return tax().default_location.name


DEFAULT_TIME_ZONE = 'America/New_York'
TIME_ZONE_AREAS = ('Africa/', 'America/', 'Antarctica/', 'Asia/', 'Atlantic/', 'Australia/', 'Europe/', 'Indian/', 'Pacific/')


def time_zone_names():
    """The time zones offered in setup: the area/city names (no legacy "US/Eastern" or "Etc/GMT+5" aliases), then UTC."""
    from zoneinfo import available_timezones
    names = sorted(name for name in available_timezones() if name.startswith(TIME_ZONE_AREAS))
    return (names or [DEFAULT_TIME_ZONE]) + ['UTC']


def apply_time_zone():
    """Use the Business time zone as the app's TIME_ZONE. Called once at startup (config/wsgi.py), so a time zone
    changed in setup takes effect when the app restarts. Blank or unknown = the TIME_ZONE in the settings file stays."""
    import os
    import time
    from zoneinfo import available_timezones

    from django.db import connection
    from django.utils import timezone

    from .models import Business
    try:
        name = Business.objects.values_list('time_zone', flat=True).first()
    except UNREADABLE:
        return None
    finally:
        if not connection.in_atomic_block:
            connection.close()   # don't carry a startup connection into forked workers
    if not name or name not in available_timezones() or name == settings.TIME_ZONE:
        return None
    settings.TIME_ZONE = name
    os.environ['TZ'] = name   # as Django does for the settings file's TIME_ZONE
    if hasattr(time, 'tzset'):
        time.tzset()
    timezone.get_default_timezone.cache_clear()
    return name


def register_rules():
    """Saved register policy, with the same transaction/request invalidation as Business."""
    from .models import RegisterRules
    cache = _settings_cache()
    if 'register_rules' not in cache:
        defaults = getattr(settings, 'REGISTER_RULES_TEST_DEFAULTS', {})
        try:
            row, _ = RegisterRules.objects.get_or_create(pk=1, defaults=defaults)
        except AssertionError:
            if not getattr(settings, 'REGISTER_RULES_TEST_ANONYMOUS', False):
                raise
            return RegisterRules(**defaults)
        cache['register_rules'] = row
    return cache['register_rules']
