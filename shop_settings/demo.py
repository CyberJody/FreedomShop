# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generated home and hardware store data. All money and stock effects use the application's services.

Source IDs reserve the demo- namespace. Child rows without source IDs belong to their
source-tagged parent. Truck sessions have no such parent, so reset refuses those.
"""
import math
import random
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from customers.models import Customer
from purchasing.models import Department, PurchaseDocument, TruckSession, Vendor
from sales.models import Document, InventoryAdjustment, Item, ItemTrash, money
from .config import business, clear_cache, ensure_configured, tax
from .models import Business
from .setup import DemoRefused, catalog_in_use, save_tax

CASHIERS = ('Demo Aspen', 'Demo Birch', 'Demo Cedar', 'Demo Willow')
DEPARTMENTS = (
    ('Paint', ('Interior Paint 5 Gal', 'Exterior Stain 5 Gal', 'Paint Sprayer', 'Drop Cloth Kit')),
    ('Hand Tools', ('Hammer Set', 'Socket Set', 'Wrench Set', 'Tool Chest')),
    ('Power Tools', ('Cordless Drill', 'Circular Saw', 'Miter Saw', 'Shop Vacuum')),
    ('Fasteners', ('Screw Assortment', 'Anchor Kit', 'Bolt Bin', 'Framing Nailer')),
    ('Plumbing', ('Kitchen Faucet', 'Water Heater', 'Sump Pump', 'Toilet')),
    ('Electrical', ('Breaker Panel', 'Wire Spool', 'Generator', 'Outlet Pack')),
    ('Lighting', ('Ceiling Fan', 'Pendant', 'Floor Lamp', 'Work Light')),
    ('Garden', ('Lawn Mower', 'Wheelbarrow', 'Garden Hose', 'Pruning Set')),
    ('Housewares', ('Cookware Set', 'Storage Cabinet', 'Step Stool', 'Trash Can')),
    ('Outdoor Living', ('Patio Set', 'Gas Grill', 'Fire Pit', 'Porch Bench')),
    ('Clearance', ('Display Grill', 'Open-Box Saw', 'Shelf Unit', 'Step Ladder')),
    ('Services', ('Key Cutting', 'Paint Mixing', 'Tool Sharpening', 'Local Delivery')),
)
COLLECTIONS = ('Alder', 'Bramble', 'Clover', 'Drift', 'Elm', 'Fern', 'Grove', 'Hazel', 'Ivy', 'Juniper', 'Kestrel', 'Larch', 'Meadow', 'Nook', 'Orchard', 'Pebble', 'Quartz', 'Reed', 'Spruce', 'Thistle', 'Umber')
# Invented word-pair identities, not a list of real people.
GIVEN = ('Aven', 'Brel', 'Cavo', 'Dren', 'Elvo', 'Fennel', 'Glim', 'Havo', 'Iven', 'Jori', 'Kelvo', 'Lumo')
FAMILY = ('Amberleaf', 'Birchvale', 'Cloudmere', 'Dapplebrook', 'Emberglade', 'Fernridge', 'Glowfield', 'Hazelmarsh', 'Ivystone', 'Juniperbay')


def scaled(count, days, minimum=1):
    return max(minimum, math.ceil(count * days / 120))


def reset_demo():
    """Refuse mixed ownership before deleting anything; never wipe a database to reset it."""
    roots = (Document, PurchaseDocument, Item, Customer, Department, Vendor)
    for model in roots:
        # SQLite LIKE is case-insensitive; ownership must use the literal prefix.
        if any(not (source or '').startswith('demo-') for source in model.objects.values_list('source_id', flat=True)):
            raise DemoRefused(f'Reset refused: {model._meta.verbose_name_plural} include rows without demo source IDs. No rows were deleted.')
    if TruckSession.objects.exists():
        raise DemoRefused('Reset refused: truck sessions have no source_id and cannot be safely demo-tagged. Use a fresh test database; no rows were deleted.')
    if InventoryAdjustment.objects.exists() or ItemTrash.objects.exists():
        raise DemoRefused('Reset refused: inventory adjustments or trash have no demo source IDs. No rows were deleted.')
    with transaction.atomic():
        # Protected links require child documents first. Return links are RESTRICT,
        # so delete every demo sales document together in one collection.
        PurchaseDocument.objects.filter(kind='voucher', source_id__startswith='demo-').delete()
        PurchaseDocument.objects.filter(source_id__startswith='demo-').delete()
        Document.objects.filter(source_id__startswith='demo-').delete()
        Item.objects.filter(source_id__startswith='demo-').delete()
        Customer.objects.filter(source_id__startswith='demo-').delete()
        Department.objects.filter(source_id__startswith='demo-').delete()
        Vendor.objects.filter(source_id__startswith='demo-').delete()


class DemoLoader:
    def __init__(self, *, days=120, seed=7, with_history=False, force=False, preserve_settings=False, progress=None):
        if not 1 <= days <= 3660:
            raise DemoRefused('Days must be between 1 and 3660.')
        self.days, self.seed, self.with_history = days, seed, with_history
        self.force, self.preserve_settings = force, preserve_settings
        self.progress = progress or (lambda message: None)
        self.rng = random.Random(seed)
        self.today = timezone.localdate()
        self.dates = [self.today - timedelta(days=i) for i in range(days, 0, -1)
                      if (self.today - timedelta(days=i)).weekday() < 5]
        if with_history and not self.dates:
            raise DemoRefused('This date range has no business days. Choose more days.')
        batch = 1
        while Customer.objects.filter(source_id__startswith=f'demo-{batch:04d}-').exists():
            batch += 1
        self.prefix = f'demo-{batch:04d}-'
        self.serial = 0
        self.events = []
        self.logo_name = None
        self.discount_reason = ''

    def token(self):
        self.serial += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f'freedomshop/{self.prefix}/{self.seed}/{self.serial}')

    def tag(self, obj):
        source = f'{self.prefix}{obj._meta.model_name}-{obj.pk}'
        type(obj).objects.filter(pk=obj.pk).update(source_id=source)
        obj.source_id = source
        if hasattr(obj, 'lines'):
            for line in obj.lines.all():
                type(line).objects.filter(pk=line.pk).update(source_id=f'{source}-line-{line.pk}')
        return obj

    def stamp(self, doc, when, *, new=True):
        fields = {'updated_at': when}
        if new:
            fields['created_at'] = when
        for field in ('completed_at', 'posted_at'):
            if getattr(doc, field, None):
                fields[field] = when
        type(doc).objects.filter(pk=doc.pk).update(**fields)
        for field, value in fields.items():
            setattr(doc, field, value)
        for relation in ('stockmovement_set', 'receivingmovement_set', 'overrides'):
            if hasattr(doc, relation):
                getattr(doc, relation).all().update(created_at=when)
        return self.tag(doc)

    def schedule(self, index, count, callback):
        day = self.dates[min(len(self.dates) - 1, index * len(self.dates) // max(count, 1))]
        when = timezone.make_aware(datetime.combine(day, time(10 + self.rng.randrange(8), self.rng.randrange(50))))
        self.events.append((when, len(self.events), callback))

    def configure(self):
        ensure_configured()
        if self.preserve_settings:
            return
        row = business()
        row.name = row.legal_name = 'Demo Home & Hardware'
        row.address_line1, row.city, row.phone = '123 Main St', 'Springfield', '555-0100'
        row.short_code, row.serial_prefix = 'DEMO', 'FS'
        row.live = False
        logo = Path(__file__).parent / 'fixtures' / 'demo-logo.png'
        # Storage owns its generated filename; never overwrite an existing upload.
        row.logo.save('demo-logo.png', ContentFile(logo.read_bytes()), save=False)
        self.logo_name = row.logo.name
        row.save()
        save_tax(dict(charge_tax=True, location_name='Local', default_rate=Decimal('7'),
                      tax_year_start_month=1, filing_frequency='quarterly'))
        clear_cache()

    def catalog(self):
        config = tax()
        # Reuse configured categories; no fixture IDs or real-shop values.
        from .models import TaxCategory
        taxable = TaxCategory.objects.get(name=config.taxable_category.name)
        non = TaxCategory.objects.filter(taxable=False).first()
        departments = []
        for i, (name, _) in enumerate(DEPARTMENTS):
            category = non if name == 'Services' else taxable
            departments.append(Department(source_id=f'{self.prefix}department-{i}', code=f'D{self.prefix[5:9]}{i:02}', name=name,
                                          tax_category=category, tax_rate=config.line_rate if name != 'Services' else 0))
        Department.objects.bulk_create(departments)
        vendors = [Vendor(source_id=f'{self.prefix}vendor-{i}', code=f'V{self.prefix[5:9]}{i:02}',
                          company=f'{COLLECTIONS[i]} Trade Supply', contact=f'{GIVEN[i % 12]} {FAMILY[i % 10]}',
                          phone=f'555-01{i:02}', email=f'vendor{i}@example.com', street=f'{i + 20} Sample Lane',
                          city='Springfield', terms='Net 30') for i in range(15)]
        Vendor.objects.bulk_create(vendors)
        items = []
        stock_scale = max(1, math.ceil(self.days / 120)) if self.with_history else 1
        for i in range(250):
            department = departments[i % 12]
            group = i // 12
            noun = DEPARTMENTS[i % 12][1][group % 4]
            name = f'{COLLECTIONS[group]} {noun}'
            cost = money(self.rng.randrange(20, 650) if department.name != 'Services' else self.rng.randrange(10, 60))
            price = money(cost * Decimal('2.2') + Decimal('.99'))
            vendor = vendors[i % 15]
            items.append(Item(source_id=f'{self.prefix}item-{i}', sku=f'D{self.prefix[5:9]}-{i + 1:04}', name=name,
                              description=f'{name}, {("satin nickel", "matte black", "brushed steel", "white")[group % 4]} finish. Sample stock for demonstration.',
                              price=price, price_2=money(price * Decimal('.95')), price_3=money(price * Decimal('.90')),
                              on_hand=0 if i % 17 == 0 else stock_scale * self.rng.randrange(35, 70), reorder_point=self.rng.randrange(2, 9),
                              item_type='noninventory' if department.name == 'Services' else 'inventory',
                              department=department, tax_rate=department.tax_rate, tax_category=department.category_name,
                              primary_vendor=vendor, vendor=vendor.company, alu=f'{vendor.code}-F{i + 1:04}',
                              order_cost=cost, average_cost=cost, use_serial_numbers=i % 9 == 0))
        Item.objects.bulk_create(items)
        customers = []
        for i in range(120):
            first, last = GIVEN[i % 12], FAMILY[i // 12]
            exempt = i in (16, 38, 62, 87, 109)
            certificate = f'DEMO-EX-{i:04}' if exempt else ''
            customers.append(Customer(source_id=f'{self.prefix}customer-{i}', customer_number=f'D{self.prefix[5:9]}{i + 1:04}',
                                      first_name=first, last_name=last, full_name=f'{first} {last}', phone=f'555-{100 + i:04}',
                                      email=f'customer{i}@example.com', street=f'{i + 1} Sample Way', city='Springfield',
                                      customer_type='Demo household', charge_account=i < 12, account_limit=Decimal('999999'),
                                      tax_exempt=exempt, tax_location=config.location_for(exempt),
                                      notes=f'Fictional demo customer. Exemption certificate: {certificate}' if exempt else 'Fictional demo customer.',
                                      source_data={'demo': True, 'exemption_certificate': certificate}, source_sha256=''))
        Customer.objects.bulk_create(customers)
        self.items, self.vendors, self.customers = items, vendors, customers
        self.stocked = [item for item in items if item.on_hand > 0]

    def sale(self, when, customer, rows, *, payments=None, method='cash', **extra):
        from sales.services import save_document
        # A held preview lets the same service calculate all rounding and discounts.
        # Finish that receipt using its actual calculated total; never duplicate tax math here.
        cashier = CASHIERS[self.serial % 4]
        data = dict(action='hold', customer_id=customer.pk, lines=rows, hold_until='none', **extra)
        doc = save_document(data, 'sale', self.token(), cashier=cashier)
        rows = [dict(row, id=line.pk) for row, line in zip(rows, doc.lines.all())]
        data.update(action='complete', version=doc.version, lines=rows,
                    payments=payments(doc.total) if payments else [dict(method=method, amount=str(doc.total),
                        reference=f'DEMO-{self.serial:05}', tender_name='Visa' if method == 'credit' else '')])
        doc = save_document(data, 'sale', doc.token, cashier=cashier)
        return self.stamp(doc, when)

    def rows(self, count=2):
        chosen = self.rng.sample(self.stocked, count)
        # Keep the sample free of negative stock for other seeds and longer windows,
        # including two-piece order pickups. Services remain the stock authority.
        if Item.objects.filter(pk__in=[item.pk for item in chosen], item_type='inventory', on_hand__lt=2).exists():
            available = set(Item.objects.filter(pk__in=[item.pk for item in self.stocked], on_hand__gte=2).values_list('pk', flat=True))
            eligible = [item for item in self.stocked if item.pk in available]
            if len(eligible) < count:
                raise DemoRefused('Sample stock is exhausted. Shorten the history window or choose another seed.')
            chosen = self.rng.sample(eligible, count)
        return [dict(item_id=item.pk, quantity='1', price=str(item.price))
                for item in chosen]

    def receipt(self, when, index):
        from sales.services import save_document
        customer = self.customers[index % 120]
        rows = self.rows(1 + self.rng.randrange(3))
        method = ('cash', 'credit', 'check', 'credit', 'cash')[index % 5]
        if index % 10 == 0:
            customer, method = self.customers[index % 12], 'account'
        extra = dict(discount_percent='5', discount_reason=self.discount_reason) if index % 7 == 0 else {}
        if index % 13 == 0:
            rows[0].update(discount_percent='10', discount_reason=self.discount_reason)
        split = (lambda total: [dict(method='cash', amount=str(money(total / 2))),
                                dict(method='credit', amount=str(total - money(total / 2)), tender_name='MasterCard')]) if index % 11 == 0 and method != 'account' else None
        doc = self.sale(when, customer, rows, payments=split, method=method, **extra)
        if index % 100 == 99:
            doc = save_document(dict(action='cancel', version=doc.version), 'sale', doc.token,
                                cashier=CASHIERS[index % 4], override={'reason': 'Demo void', 'by': 'Demo loader'})
            self.stamp(doc, when, new=False)
        elif index % 17 == 0:  # 5.9% linked returns, with original prices and tax rates
            original = doc.lines.first()
            self.sale(when + timedelta(minutes=1), customer,
                      [dict(returned_from_line=original.pk, quantity='-1', return_reason='other', return_note='Demo return')],
                      method='account' if method == 'account' else 'cash')

    def order(self, when, index):
        from sales.services import fulfill_order, save_document, save_order_payment
        customer = self.customers[(index + 20) % 120]
        rows = self.rows(2)
        for row in rows:
            row['quantity'] = '2'
        order = save_document(dict(action='save', customer_id=customer.pk, lines=rows,
                                   due_date=(when.date() + timedelta(days=14)).isoformat(),
                                   discount_percent='5' if index % 4 == 0 else '0', discount_reason=self.discount_reason),
                              'order', self.token(), cashier=CASHIERS[index % 4])
        self.stamp(order, when)
        deposit = money(order.total / 2)
        order = save_order_payment(order.pk, dict(version=order.version, token=self.token(), amount=str(deposit), method='credit', card_type='Visa'), actor={'cashier': CASHIERS[index % 4]})
        order.payments.all().update(created_at=when + timedelta(minutes=1))
        if index % 5 == 0:
            order = save_document(dict(action='cancel', version=order.version, refund_deposit=True,
                                       deposit_refund_method='cash', deposit_refund_amount=str(order.unused_payments)),
                                  'order', order.token, cashier=CASHIERS[index % 4])
            order.payments.filter(amount__lte=0).update(created_at=when + timedelta(minutes=2))
        elif index % 5 in (1, 2, 3):
            partial = index % 5 == 1
            if not partial:
                order = save_order_payment(order.pk, dict(version=order.version, token=self.token(), amount=str(order.balance_due), method='cash'), actor={'cashier': CASHIERS[(index + 1) % 4]})
                order.payments.filter(created_at__gt=when + timedelta(minutes=1)).update(created_at=when + timedelta(minutes=2))
            pickup = dict(version=order.version, token=self.token())
            pickup.update({f'qty_{line.pk}': '1' if partial else str(line.quantity) for line in order.lines.all()})
            receipt = fulfill_order(order.pk, pickup, cashier=CASHIERS[(index + 1) % 4])
            self.stamp(receipt, when + timedelta(minutes=3))
        self.stamp(order, when + timedelta(minutes=3), new=False)

    def purchase(self, when, index, receive_count):
        from purchasing import piece_labels, truck
        from purchasing.models import PieceLabel
        from purchasing.services import save_purchase
        vendor = self.vendors[index % 15]
        items = [item for item in self.stocked if item.primary_vendor_id == vendor.pk and item.item_type == 'inventory'][:3]
        header = dict(token=self.token(), version=0, vendor=vendor, notes='Demo stock replenishment', terms='Net 30',
                      ship_date=when.date() + timedelta(days=7), payment_due=None, invoice_number='')
        rows = [dict(item=item, quantity=Decimal('3'), cost=item.order_cost) for item in items]
        po = save_purchase(header, rows, 'po', 'save')
        self.stamp(po, when)
        if index < 5:
            piece_labels.labels_for_print(po)  # Generates serials only; never dispatches to a printer.
            PieceLabel.objects.filter(line__document=po).update(created_at=when)
        if index >= receive_count:
            return
        if index < 5:
            session = TruckSession.objects.create(token=self.token(), notes='Demo truck receiving', created_by_name=CASHIERS[index % 4])
            TruckSession.objects.filter(pk=session.pk).update(created_at=when)
            for label in PieceLabel.objects.filter(line__document=po):
                result = truck.scan(session, label.code, by_name=CASHIERS[index % 4])
                if result['kind'] != 'ok':
                    raise DemoRefused(result['message'])
            vouchers = truck.receive(session)
            session.scans.update(scanned_at=when + timedelta(minutes=1))
            TruckSession.objects.filter(pk=session.pk).update(received_at=when + timedelta(minutes=2))
        else:
            receiving = [dict(item=line.item, quantity=line.quantity if index % 4 else Decimal('2'), cost=line.cost, source_line=line.pk)
                         for line in po.lines.select_related('item')]
            header.update(token=self.token(), invoice_number=f'DEMO-{index:04}')
            vouchers = [save_purchase(header, receiving, 'voucher', 'post', source_po=po)]
        for voucher in vouchers:
            self.stamp(voucher, when + timedelta(minutes=2))

    def specials(self, when, index):
        from sales.services import save_document
        customer = self.customers[index % 12]
        # Charge first, then take a smaller payment; balances arise from real activity.
        self.sale(when, customer, self.rows(1), method='account')
        self.sale(when + timedelta(minutes=1), customer, [dict(account_payment=True, price='20', quantity='1')], method='cash')
        number = f'{self.prefix}gift-{index:03}'
        self.sale(when + timedelta(minutes=2), customer,
                  [dict(item_name='Gift Certificate', description='Demo gift certificate', certificate_number=number,
                        quantity='1', price='100', tax_rate='0')], method='credit')
        self.sale(when + timedelta(minutes=3), customer, self.rows(1),
                  payments=lambda total: [dict(method='gift', amount=str(min(total, Decimal('25'))), reference=number)] +
                      ([dict(method='cash', amount=str(total - min(total, Decimal('25'))))] if total > 25 else []))
        held = save_document(dict(action='hold', customer_id=customer.pk, lines=self.rows(1), hold_until='1d' if index % 2 == 0 else 'none'),
                             'sale', self.token(), cashier=CASHIERS[index % 4])
        self.stamp(held, when)
        if index % 2 == 0:
            Document.objects.filter(pk=held.pk).update(hold_until=when + timedelta(days=1))

    def run(self):
        refusal = catalog_in_use()
        if refusal and not self.force:
            raise DemoRefused(refusal + ' Use --force only to append demo data to an intentional test copy.')
        if not self.force and (Document.objects.exists() or PurchaseDocument.objects.exists() or TruckSession.objects.exists()):
            raise DemoRefused('Demo data requires an empty shop, including sales and purchasing history.')
        try:
            with transaction.atomic():
                # Serialize competing demo loads and repeat the guard after obtaining the lock.
                ensure_configured()
                list(Business.objects.select_for_update().all())
                if not self.force and catalog_in_use():
                    raise DemoRefused(catalog_in_use())
                self.configure()
                self.catalog()
                self.progress('Loaded 12 departments, 250 items, 15 vendors and 120 customers.')
                if self.with_history:
                    from sales.models import DiscountReason
                    reason = DiscountReason.objects.filter(active=True).order_by('pk').first()
                    if reason is None:
                        reason, _ = DiscountReason.objects.get_or_create(name='Demo promotion', defaults={'active': True})
                    if not reason.active:
                        raise DemoRefused('Activate the Demo promotion discount reason before loading history.')
                    self.discount_reason = reason.name
                    receipts, orders = scaled(1500, self.days), scaled(60, self.days, 5)
                    pos, receiving = scaled(40, self.days, 5), scaled(30, self.days, 3)
                    for i in range(receipts):
                        self.schedule(i, receipts, lambda when, i=i: self.receipt(when, i))
                    for i in range(orders):
                        self.schedule(i, orders, lambda when, i=i: self.order(when, i))
                    for i in range(pos):
                        self.schedule(i, pos, lambda when, i=i: self.purchase(when, i, receiving))
                    for i in range(scaled(12, self.days, 4)):
                        self.schedule(i, scaled(12, self.days, 4), lambda when, i=i: self.specials(when, i))
                    for index, (when, _, callback) in enumerate(sorted(self.events)):
                        callback(when)
                        if index and index % 250 == 0:
                            self.progress(f'Generated {index} history events.')
                    from sales.holds import expire_overdue_holds
                    expire_overdue_holds(receipts=Document.objects.filter(source_id__startswith=self.prefix))
                counts = dict(items=len(self.items), customers=len(self.customers), vendors=len(self.vendors), departments=12,
                              receipts=Document.objects.filter(source_id__startswith=self.prefix, kind='sale').count(),
                              orders=Document.objects.filter(source_id__startswith=self.prefix, kind='order').count(),
                              purchase_orders=PurchaseDocument.objects.filter(source_id__startswith=self.prefix, kind='po').count(),
                              vouchers=PurchaseDocument.objects.filter(source_id__startswith=self.prefix, kind='voucher').count())
            return counts
        except Exception:
            if self.logo_name:
                Business._meta.get_field('logo').storage.delete(self.logo_name)
            raise
        finally:
            clear_cache()
