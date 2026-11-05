# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import models


def tax_location_choices():
    """The Tax Location choices for a customer: the shop's default location and its exempt one (Tax Settings)."""
    from shop_settings.config import tax
    return tax().customer_choices


def tax_location_for(exempt):
    """The location name the Tax Exempt checkbox gives: the exempt location, else the default one (Tax Settings)."""
    from shop_settings.config import tax
    return tax().location_for(exempt)


class Customer(models.Model):
    source_id = models.CharField(max_length=100, unique=True)
    customer_number = models.CharField(max_length=100, blank=True, db_index=True)   # next-number lookup, sort
    full_name = models.CharField(max_length=500, blank=True)
    first_name = models.CharField(max_length=200, blank=True)
    last_name = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=50, blank=True)
    track_as_company = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    alternate_phone = models.CharField(max_length=100, blank=True)
    alternate_phone_2 = models.CharField(max_length=100, blank=True)
    alternate_contact = models.CharField(max_length=200, blank=True)
    customer_type = models.CharField(max_length=100, blank=True)
    # QB POS tax location: a Tax Settings location name (the default one or the exempt one). Blank (most imported
    # customers) means the default location.
    # Kept in step with tax_exempt: the Tax Exempt checkbox decides.
    tax_location = models.CharField(max_length=100, blank=True)
    tax_exempt = models.BooleanField(default=False)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    accept_checks = models.BooleanField(default=True)
    email_opt_in = models.BooleanField(default=False)
    do_not_mail = models.BooleanField(default=False)
    company = models.CharField(max_length=500, blank=True)
    phone = models.CharField(max_length=100, blank=True)
    mobile = models.CharField(max_length=100, blank=True)
    email = models.CharField(max_length=500, blank=True)
    street = models.TextField(blank=True)
    city = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    # QuickBooks Options (QB POS Customer Detail): Use with QuickBooks, Charge Account, Account Limit / Balance.
    use_with_quickbooks = models.BooleanField(default=False)
    charge_account = models.BooleanField(default=False)
    account_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Account balance and past-due amount as QB POS held them in the backup (CreditUsed, Credit30/60/90Overdue),
    # rounded half-up to whole cents. Charges and payments recorded in this app are added on top.
    imported_account_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False)
    imported_past_due = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False)
    imported_last_sale = models.DateField(null=True, blank=True, editable=False)
    # Net of charges (+) and payments on account (-) on receipts completed in this app; recomputed from those receipts
    # by sales.accounts.refresh whenever one is saved or cancelled. Balance = imported balance + this.
    app_account_activity = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False)
    version = models.PositiveIntegerField(default=1)
    locally_edited_at = models.DateTimeField(null=True, editable=False)
    source_data = models.JSONField(default=dict)
    source_sha256 = models.CharField(max_length=64)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['full_name', 'source_id']

    # (matching QB): QB POS gives every customer a Customer # when it is created. QB's numbers are EAN-13 barcodes
    # (12 digits + a check digit; 38,232 of the 38,291 imported numbers, all in QB's '0400001' series, e.g.
    # 0400001713105). Customers created in this app ('local:' source ids) get QB's next one: the highest valid EAN-13
    # number on file, its first 12 digits + 1, with the check digit. Hand-typed QB oddities (14 digits, bad check
    # digit, 'Q') are skipped. With no EAN-13 numbers on file it falls back to the highest all-digit number + 1.
    LOCAL_PREFIX = 'local:'
    NUMBER_LOCK = 530053   # Postgres advisory lock key: one number at a time across workers

    @property
    def app_made(self):
        return (self.source_id or '').startswith((self.LOCAL_PREFIX, 'demo-'))

    @staticmethod
    def ean13_check_digit(first12):
        total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
        return str((10 - total % 10) % 10)

    @classmethod
    def is_ean13(cls, number):
        return len(number) == 13 and number.isdigit() and cls.ean13_check_digit(number[:12]) == number[12]

    @classmethod
    def next_customer_number(cls):
        """QB's next Customer # (see above), as text. Call inside a transaction holding `lock_numbers`."""
        from django.db.models import BigIntegerField
        from django.db.models.functions import Cast
        candidate = None
        # 13 all-digit characters sort as numbers; the first valid one from the top is QB's newest number.
        for number in (cls.objects.filter(customer_number__regex=r'^[0-9]{13}$').order_by('-customer_number')
                       .values_list('customer_number', flat=True).iterator(chunk_size=50)):
            if cls.is_ean13(number):
                first12 = str(int(number[:12]) + 1).zfill(12)
                candidate = first12 + cls.ean13_check_digit(first12)
                break
        if candidate is None:
            top = (cls.objects.filter(customer_number__regex=r'^[0-9]{1,18}$')
                   .annotate(n=Cast('customer_number', BigIntegerField()))
                   .order_by('-n', '-customer_number').values_list('customer_number', 'n').first())
            return str(top[1] + 1).zfill(len(top[0])) if top else '1'
        while cls.objects.filter(customer_number=candidate).exists():   # never reuse a number already on file
            first12 = str(int(candidate[:12]) + 1).zfill(12)
            candidate = first12 + cls.ean13_check_digit(first12)
        return candidate

    @classmethod
    def lock_numbers(cls):
        """Serialize number assignment until the current transaction ends (Postgres; SQLite writes are serial)."""
        from django.db import connection
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [cls.NUMBER_LOCK])

    def save(self, *args, **kwargs):
        if self.pk is None and not self.customer_number and self.app_made:
            from django.db import transaction
            with transaction.atomic():
                self.lock_numbers()
                self.customer_number = self.next_customer_number()
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

    @property
    def composed_name(self):
        return ' '.join(value for value in (self.title, self.first_name, self.last_name) if value)

    @property
    def display_name(self):
        return (self.company if self.track_as_company else '') or self.full_name or self.company or self.customer_number or self.source_id

    @property
    def account_balance(self):
        """Balance owed on the customer's charge account (negative = credit): QB balance plus activity in this app."""
        return self.imported_account_balance + self.app_account_activity

    @property
    def available_credit(self):
        """Account Limit minus balance; None when no limit is set (QB POS shows it blank)."""
        return self.account_limit - self.account_balance if self.account_limit else None

    PHONE_FIELDS = ('phone', 'mobile', 'alternate_phone', 'alternate_phone_2')

    # QB's 4th phone field mostly holds 5-digit numbers and first names (170 of 193 customers), and a few phone
    # fields hold emails or "UNLISTED": a value counts as a phone only with at least this many digits.
    MIN_PHONE_DIGITS = 7

    def phone_numbers(self):
        """Every phone on file, in order phone, mobile, alternate, additional; blanks and non-phones dropped and
        duplicates (same digits, e.g. 518-286-0777 and (518) 286-0777) printed once (phones aren't entered
        consistently)."""
        numbers, seen = [], set()
        for field in self.PHONE_FIELDS:
            value = (getattr(self, field) or '').strip()
            key = ''.join(ch for ch in value if ch.isdigit())
            if len(key) >= self.MIN_PHONE_DIGITS and key not in seen:
                seen.add(key)
                numbers.append(value)
        return numbers

    @property
    def has_address(self):
        """A billing address worth printing: street or city on file (asks for one when both are blank)."""
        return bool((self.street or '').strip() or (self.city or '').strip())

    def address_lines(self):
        """Street line(s) then 'City ST zip'; blanks dropped. The one shape receipts and sales orders print."""
        lines = [line.strip() for line in (self.street or '').splitlines()]
        lines.append(' '.join(v.strip() for v in [self.city, self.state, self.postal_code] if v and v.strip()))
        return [line for line in lines if line]

    @property
    def street_lines(self):
        lines = (self.street or '').splitlines()
        return (lines[0] if lines else '', ' '.join(lines[1:]))

    @property
    def default_shipping_address(self):
        address = self.addresses.filter(is_default=True).first()
        if address:
            return address.address
        if self.do_not_mail:
            return ''
        return '\n'.join(v for v in [self.display_name, self.street, ' '.join(filter(None, [self.city, self.state, self.postal_code]))] if v)


class CustomerAddress(models.Model):
    customer = models.ForeignKey(Customer, related_name='addresses', on_delete=models.CASCADE)
    label = models.CharField(max_length=100, default='Shipping')
    address = models.TextField(max_length=2000)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['customer'], condition=models.Q(is_default=True), name='one_default_customer_address')]
