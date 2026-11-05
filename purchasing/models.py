# SPDX-License-Identifier: AGPL-3.0-or-later
from config.provenance import imported_source
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models

from sales.models import MoneyField, line_amount, money, rate_text
from shop_settings.config import default_tax_category, default_tax_rate
from shop_settings.fields import TaxCategoryField


class LegacyMaster(models.Model):
    source_id = models.CharField(max_length=100, null=True, blank=True, unique=True, editable=False)
    source_data = models.JSONField(default=dict, blank=True, editable=False)
    source_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    imported_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        abstract = True


class Vendor(LegacyMaster):
    contact = models.CharField(max_length=200, blank=True)
    account_number = models.CharField(max_length=100, blank=True)
    fax = models.CharField(max_length=100, blank=True)
    freight_rate = models.CharField(max_length=200, blank=True)
    code = models.CharField(max_length=40, blank=True)
    company = models.CharField(max_length=200)
    phone = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    street = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=40, blank=True)
    terms = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['company', 'pk']
        constraints = [models.UniqueConstraint(fields=['code'], condition=~models.Q(code=''), name='vendor_nonblank_code_unique')]

    def __str__(self):
        return f'{self.code} · {self.company}' if self.code else self.company

    @property
    def details_text(self):
        """(matching QB's vendor details): what a clerk calling the vendor needs that isn't a Vendor List column,
        one 'Label: value' line each, blanks left out. The list's Show Details panel adds these under the row's
        columns (Contact, Phone and E-Mail are columns)."""
        extra = (('Fax', self.fax), ('Account #', self.account_number), ('Terms', self.terms), ('Freight rate', self.freight_rate))
        return '\n'.join(f'{label}: {value.strip()}' for label, value in extra if (value or '').strip())


class Department(LegacyMaster):
    # Tax Settings category (assigning a name such as "Non" picks or creates the row, as the old text field took it).
    tax_category = TaxCategoryField('shop_settings.TaxCategory', null=True, blank=True, default=default_tax_category, on_delete=models.PROTECT, related_name='departments')
    legacy_tax_category = models.CharField(max_length=100, null=True, editable=False)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, default=default_tax_rate)
    margin = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    markup = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    code = models.CharField(max_length=40, blank=True)
    name = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['name', 'pk']
        constraints = [models.UniqueConstraint(fields=['code'], condition=~models.Q(code=''), name='department_nonblank_code_unique')]

    def __str__(self):
        return f'{self.code} · {self.name}' if self.code else self.name

    @property
    def tax_label(self):
        """(matching QB's Department List): the tax code with the rate the department gives its items, e.g.
        "Taxable Sales · 7%" or "Non · 0%". The rate is the department's own tax_rate, the one Add Item / Add Special
        Order Item copy onto a new item in this department."""
        rate = '' if self.tax_rate is None else f'{rate_text(self.tax_rate)}%'
        return ' · '.join(part for part in (self.category_name, rate) if part)

    @property
    def category_name(self):
        return self.tax_category.name if self.tax_category_id else ''


class ItemVendor(models.Model):
    item = models.ForeignKey('sales.Item', on_delete=models.CASCADE, related_name='alternate_vendors')
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT)
    order_cost = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True)
    upc = models.CharField(max_length=100, blank=True)
    alu = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['item', 'vendor'], name='unique_item_alternate_vendor')]


class PurchaseDocument(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=10, choices=[('po','Purchase Order'),('voucher','Receiving Voucher')])
    status = models.CharField(max_length=12, choices=[('open','Open'),('pending','Pending'),('closed','Closed'),('held','Held'),('posted','Received'),('cancelled','Cancelled')])
    # Null only on imported documents whose vendor was deleted in QuickBooks; vendor_name keeps the snapshot.
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, null=True)
    vendor_name = models.CharField(max_length=200)
    source_po = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='vouchers')
    source_order = models.ForeignKey('sales.Document', null=True, blank=True, on_delete=models.PROTECT, related_name='purchase_orders')
    # QB backup provenance, or reserved demo- IDs for generated native documents.
    source_id = models.CharField(max_length=100, null=True, blank=True, unique=True, editable=False)
    source_number = models.CharField(max_length=40, blank=True, db_index=True, editable=False)
    clerk = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    terms = models.CharField(max_length=200, blank=True)
    # Shown and printed as "Preferred Pickup Date"; imported POs keep QB's Ship Date here.
    ship_date = models.DateField(null=True, blank=True)
    alternate_pickup_date = models.DateField(null=True, blank=True)  # second truck date for the builder
    payment_due = models.DateField(null=True, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    freight = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-created_at','-pk']
        constraints = [models.CheckConstraint(condition=models.Q(kind='po', status__in=['open','pending','closed','cancelled']) | models.Q(kind='voucher', status__in=['held','posted','cancelled']), name='purchase_kind_state')]

    @property
    def imported(self):
        return imported_source(self.source_id)

    @property
    def number(self):
        if self.source_number:
            return f'{"PO" if self.kind == "po" else "RV"}-{self.source_number}'
        return f'{"PO" if self.kind == "po" else "RV"}-{self.pk:06d}'

    @property
    def number_or_label(self):
        """Same name as sales.Document.number_or_label: the shared page heading (sales/base.html) uses it."""
        return self.number

    @property
    def customer_orders(self):
        if hasattr(self, '_customer_orders'):
            return self._customer_orders
        from sales.models import Document
        return Document.objects.filter(models.Q(lines__purchaseline__document=self) | models.Q(pk=self.source_order_id)).distinct()

    @customer_orders.setter
    def customer_orders(self, value):
        self._customer_orders = list(value)

    @property
    def ordered(self):
        return sum((line.quantity for line in self.lines.all()), Decimal('0'))

    @property
    def received(self):
        return sum((line.received for line in self.lines.all()), Decimal('0'))

    @property
    def due(self):
        if self.kind == 'po' and self.status == 'cancelled':
            return Decimal('0')   # nothing more is expected on a cancelled PO
        return self.ordered - self.received


class PurchaseLine(models.Model):
    document = models.ForeignKey(PurchaseDocument, related_name='lines', on_delete=models.CASCADE)
    source_id = models.CharField(max_length=120, null=True, blank=True, editable=False)
    position = models.PositiveIntegerField()
    # Null on imported lines whose QB item was deleted before the backup; the snapshot columns still describe it.
    item = models.ForeignKey('sales.Item', on_delete=models.PROTECT, null=True)
    sku = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=200, blank=True)
    description = models.CharField(max_length=1000)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    cost = MoneyField(max_digits=16, decimal_places=2)
    recorded_amount = MoneyField(max_digits=16, decimal_places=2, null=True, blank=True, editable=False)  # QB's line total (see sales.Line)
    received = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    # Quantity QuickBooks had received on an imported PO line at backup time; native vouchers add to it.
    imported_received = models.DecimalField(max_digits=12, decimal_places=3, default=0, editable=False)
    source_line = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='receiving_lines')
    sales_line = models.ForeignKey('sales.Line', null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['position','pk']
        # Native lines are validated; imported lines keep QuickBooks values (a few zero-quantity lines exist).
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0) | models.Q(source_id__isnull=False),name='purchase_quantity_positive'),
            models.CheckConstraint(condition=models.Q(cost__gte=0),name='purchase_cost_positive'),
            models.CheckConstraint(condition=models.Q(received__gte=0) & models.Q(received__lte=models.F('quantity')),name='purchase_received_bounds'),
        ]

    @property
    def amount(self):
        return line_amount(self.quantity, self.cost, self.recorded_amount)

    @property
    def due(self):
        return self.quantity-self.received


class PieceLabel(models.Model):
    """one code per piece ordered on a PO line, printed on the PO item label (QR code). Made the first time labels
    are printed for the PO (more pieces are added on a later print if the line's quantity grew). Never deleted or
    renumbered, so a reprint gives the same codes; the PO line can't be removed once its labels exist."""
    line = models.ForeignKey(PurchaseLine, related_name='piece_labels', on_delete=models.PROTECT)
    piece = models.PositiveIntegerField()
    code = models.CharField(max_length=16, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['line', 'piece']
        constraints = [models.UniqueConstraint(fields=['line', 'piece'], name='one_piece_label_per_line_piece'),
                       models.CheckConstraint(condition=models.Q(piece__gte=1), name='piece_label_piece_positive')]

    def __str__(self):
        return self.code


class ReceivingMovement(models.Model):
    document = models.ForeignKey(PurchaseDocument, on_delete=models.PROTECT)
    item = models.ForeignKey('sales.Item', on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['document','item'],name='one_receiving_movement_per_item')]


class ReorderIgnore(models.Model):
    """Reorder Reminders: staff ticked Ignore for this item. Temporary — cleared once the item is back above its
    reorder point (purchasing.reorder.clear_restocked) or when the box is unticked."""
    item = models.OneToOneField('sales.Item', on_delete=models.CASCADE, related_name='reorder_ignore')
    created_at = models.DateTimeField(auto_now_add=True)


class TruckSession(models.Model):
    """one truck being unloaded. Staff scan each piece's label into it; Receive Items then
    posts one ordinary receiving voucher per PO on the truck (a QB voucher receives one PO, and so does ours). The
    truck is only a list of scans until then: nothing in stock or on the POs changes while it is open."""
    STATUSES = [('open', 'Unloading'), ('received', 'Received'), ('cancelled', 'Cancelled')]
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=10, choices=STATUSES, default='open')
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_by_name = models.CharField(max_length=100, blank=True)   # the cashier ringing (shared register login)
    created_at = models.DateTimeField(auto_now_add=True)
    received_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return self.number

    @property
    def number(self):
        return f'TRUCK-{self.pk}' if self.pk else 'New truck'


class TruckScan(models.Model):
    """One piece scanned off a truck. A label is on at most one truck at a time (open or received); removing the scan
    (a damaged piece going back, a reversed voucher) frees the label so it can be scanned on a later truck."""
    session = models.ForeignKey(TruckSession, related_name='scans', on_delete=models.CASCADE)
    label = models.ForeignKey(PieceLabel, related_name='truck_scans', on_delete=models.PROTECT)
    scanned_at = models.DateTimeField(auto_now_add=True)
    scanned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    scanned_by_name = models.CharField(max_length=100, blank=True)
    removed = models.BooleanField(default=False)
    removed_reason = models.CharField(max_length=200, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    voucher = models.ForeignKey(PurchaseDocument, null=True, blank=True, on_delete=models.PROTECT, related_name='truck_scans')

    class Meta:
        ordering = ['scanned_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['label'], condition=models.Q(removed=False), name='one_live_truck_scan_per_label')]
