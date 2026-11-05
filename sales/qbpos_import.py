# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export migration service shared by Settings and import_qbpos.

The preview executes the exact load in a rolled-back transaction. Inventory
starts at the exported snapshot; historical sales never move it a second time.
No native sale, tax calculation, or ledger service is called here.
"""
import base64
import json
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from customers.models import Customer, tax_location_for
from purchasing.models import Department, PurchaseDocument, PurchaseLine, ReceivingMovement, Vendor
from shop_settings.config import NON_KEYS, TAXABLE_KEYS, category_key, tax
from shop_settings.models import TaxCategory, TaxSettings

from .models import Document, ImportLog, Item, Line, Tender, money
from .qbpos_files import ImportError, boolean, number, sale_date

ZERO = Decimal('0')


def key(value):
    return ' '.join(str(value or '').casefold().split())


def item_key(row):
    return key(row.get('name')), key(row.get('attribute')), key(row.get('size'))


def customer_name(row):
    return row.get('full_name') or ' '.join(filter(None, (row.get('first_name'), row.get('last_name')))) or row.get('company', '')


def customer_key(name, phone):
    return key(name), ''.join(char for char in phone if char.isdigit())


def source(file, row, suffix=''):
    return f'qbpos:{file["sha256"]}:{row["_row"]}{suffix}'


def snapshot(obj):
    values = {}
    for field in obj._meta.concrete_fields:
        value = getattr(obj, field.attname)
        if isinstance(value, Decimal):
            value = format(value, f'.{field.decimal_places}f')
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, (bytes, memoryview)):
            value = base64.b64encode(bytes(value)).decode('ascii')
        values[field.attname] = value
    return json.loads(json.dumps(values, cls=DjangoJSONEncoder))


def mapping_proposals(payload, options=None):
    """Read-only proposals. Unknown tax codes require an explicit category."""
    options = options or {}
    categories = list(TaxCategory.objects.filter(active=True))
    department_choices = list(Department.objects.filter(active=True))
    tax_codes, departments = {}, {}
    for row in payload.get('items', {}).get('rows', []):
        code = row.get('tax_code', '').strip()
        if code not in tax_codes:
            code_key = category_key(code)
            taxable = False if code_key in NON_KEYS else True if code_key in TAXABLE_KEYS else None
            candidate = next((c for c in categories if key(c.name) == key(code)), None)
            candidate = candidate or next((c for c in categories if taxable is not None and c.taxable == taxable), None)
            selected = options.get('tax_map', {}).get(code, candidate.pk if candidate else '')
            tax_codes[code] = {'code': code, 'selected': str(selected), 'known': candidate is not None}
        name = row.get('department') or 'Imported items'
        if name not in departments:
            matches = [dep for dep in department_choices if key(dep.name) == key(name)]
            selected = options.get('department_map', {}).get(name, matches[0].pk if len(matches) == 1 else '')
            departments[name] = {'name': name, 'selected': str(selected), 'tax_code': code}
    return {'tax_codes': list(tax_codes.values()), 'departments': list(departments.values()),
            'categories': categories, 'department_choices': department_choices}


class Loader:
    def __init__(self, payload, options):
        self.payload = payload
        self.options = options
        self.changes = []
        self.summary = {'counts': {}, 'warnings': [], 'duplicates': [], 'opening_value': '0.00',
                        'opening_vouchers': 0, 'receipts_with_tenders': 0, 'receipts_totals_only': 0}
        self.opening = defaultdict(list)
        self.vendor_codes = {}
        self.departments = {}

    def bump(self, kind, action):
        counts = self.summary['counts'].setdefault(kind, {'created': 0, 'updated': 0, 'reused': 0, 'skipped': 0})
        counts[action] += 1

    def warn(self, message):
        if message not in self.summary['warnings']:
            self.summary['warnings'].append(message)

    def save(self, model, source_id, values, kind=None):
        obj = model.objects.select_for_update().filter(source_id=source_id).first()
        before = snapshot(obj) if obj else None
        created = obj is None
        obj = obj or model(source_id=source_id)
        for field, value in values.items():
            setattr(obj, field, value)
        if before is not None and snapshot(obj) == before:
            if kind:
                self.bump(kind, 'reused')
            return obj, False
        if not created and hasattr(obj, 'version'):
            obj.version += 1
        empty = [field.name for field in obj._meta.fields if getattr(obj, field.attname) in (None, '')]
        obj.full_clean(exclude=empty)
        obj.save()
        obj.refresh_from_db()
        self.changes.append({'model': obj._meta.label_lower, 'pk': obj.pk, 'before': before, 'after': snapshot(obj)})
        if kind:
            self.bump(kind, 'created' if created else 'updated')
        return obj, created

    def child(self, model, **values):
        obj = model(**values)
        empty = [field.name for field in obj._meta.fields if getattr(obj, field.attname) in (None, '')]
        obj.full_clean(exclude=empty)
        obj.save()
        obj.refresh_from_db()
        self.changes.append({'model': obj._meta.label_lower, 'pk': obj.pk, 'before': None, 'after': snapshot(obj)})
        return obj

    def duplicate(self, kind, row, reason):
        self.summary['duplicates'].append({'kind': kind, 'row': row['_row'], 'reason': reason})
        self.bump(kind, 'skipped')

    def contact(self, row):
        return {field: row.get(field, '') for field in ('company', 'phone', 'email', 'city', 'state', 'postal_code')} | {
            'street': '\n'.join(filter(None, (row.get('street'), row.get('street2'))))}

    def vendors(self):
        file = self.payload.get('vendors')
        if file:
            existing = {key(v.company): v for v in Vendor.objects.only('pk', 'source_id', 'company', 'code')}
            for row in file['rows']:
                if not row.get('company'):
                    raise ImportError(f'vendors row {row["_row"]}: Company is required.')
                sid = source(file, row)
                owned = Vendor.objects.select_for_update().filter(source_id=sid).first()
                match = Vendor.objects.filter(code__iexact=row.get('code', '')).first() if row.get('code') else None
                match = match or existing.get(key(row['company']))
                if not owned and match and match.source_id != sid:
                    self.duplicate('vendors', row, f'Existing vendor {match.company}; reused without changes.')
                    vendor = match
                else:
                    vendor, _ = self.save(Vendor, sid, self.contact(row) | {
                        **{field: row.get(field, '') for field in ('code', 'contact', 'fax', 'account_number', 'terms')},
                        'source_data': row, 'source_sha256': file['sha256']}, 'vendors')
                    existing[key(vendor.company)] = vendor
                if row.get('code'):
                    self.vendor_codes[key(row['code'])] = vendor

    def resolve_vendor(self, file, row):
        code = row.get('vendor_code', '')
        if not code:
            return None
        if key(code) not in self.vendor_codes:
            matches = list(Vendor.objects.filter(Q(code__iexact=code) | Q(company__iexact=code)))
            if len(matches) > 1:
                raise ImportError(f'Ambiguous vendor {code!r}; give it a unique code.')
            if matches:
                self.vendor_codes[key(code)] = matches[0]
                self.bump('vendors', 'reused')
            else:
                vendor, _ = self.save(Vendor, source(file, row, ':vendor'), {'code': code, 'company': code}, 'vendors')
                self.vendor_codes[key(code)] = vendor
                self.warn(f'Vendor {code}: only its code was exported; created a placeholder. Upload the vendor list with the items for contact details.')
        return self.vendor_codes[key(code)]

    def category(self, code):
        selected = self.options['tax_map'].get(code)
        category = TaxCategory.objects.filter(pk=selected, active=True).first() if selected else None
        if category is None:
            raise ImportError(f'Choose a TaxCategory for tax code {code or "(blank)"!r} in the preview (CLI: --tax-map).')
        config = tax()
        rate = (category.default_rate if category.default_rate is not None else config.default_rate) if category.taxable and config.charge_tax else ZERO
        return category, rate

    def department(self, file, row, category, rate):
        name = row.get('department') or 'Imported items'
        if name not in self.departments:
            selected = self.options['department_map'].get(name)
            if selected:
                department = Department.objects.filter(pk=selected, active=True).first()
                if department is None:
                    raise ImportError(f'Department mapping for {name!r} is unavailable.')
                self.bump('departments', 'reused')
            else:
                department, _ = self.save(Department, source(file, row, ':dept'), {
                    'name': name, 'tax_category': category, 'tax_rate': rate,
                    'code': row.get('department_code', '')}, 'departments')
            self.departments[name] = department
        department = self.departments[name]
        if department.tax_category_id != category.pk:
            self.warn(f'Department {name} contains a different tax code; each item keeps its own mapped tax category.')
        return department

    def items(self):
        file = self.payload.get('items')
        if not file:
            return
        existing = {item_key({'name': i.name, 'attribute': i.wood, 'size': i.size}): i
                    for i in Item.objects.only('pk', 'source_id', 'name', 'wood', 'size', 'sku')}
        for row in file['rows']:
            label = f'items row {row["_row"]}'
            if not row.get('name'):
                raise ImportError(f'{label}: Item Name is required. Remove report totals rows.')
            sid = source(file, row)
            owned = Item.objects.select_for_update().filter(source_id=sid).first()
            match = existing.get(item_key(row))
            if not owned and match and match.source_id != sid:
                self.duplicate('items', row, f'Name + attribute + size matches item {match.sku}; skipped.')
                continue
            sku = row.get('sku') or f'QB-{file["sha256"][:10]}-{row["_row"]}'
            if Item.objects.filter(sku=sku).exclude(source_id=sid).exists():
                self.duplicate('items', row, f'Item number {sku} already exists; skipped.')
                continue
            category, rate = self.category(row.get('tax_code', '').strip())
            department = self.department(file, row, category, rate)
            vendor = self.resolve_vendor(file, row)
            quantity = number(row.get('quantity'), f'{label} On-Hand Qty', 3, default='0')
            cost = number(row.get('cost'), f'{label} Average Unit Cost', signed=False, default='0', digits=16)
            price = number(row.get('price'), f'{label} Regular Price', signed=False, default='0', digits=16)
            order_cost = number(row.get('order_cost'), f'{label} Order Cost', signed=False, default=str(cost), digits=16)
            if not row.get('quantity'):
                self.warn(f'{label}: missing On-Hand Qty; treated as zero.')
            if not row.get('cost'):
                self.warn(f'{label}: missing Average Unit Cost; treated as zero. Check inventory value before importing.')
            item_type = key(row.get('item_type'))
            if item_type not in ('', 'inventory', 'noninventory', 'non-inventory', 'non inventory', 'service'):
                raise ImportError(f'{label}: unsupported item type {item_type!r}; assemblies/groups require manual setup.')
            noninventory = item_type in ('noninventory', 'non-inventory', 'non inventory', 'service')
            if noninventory and quantity:
                self.warn(f'{label}: non-inventory quantity ignored.')
                quantity = ZERO
            if quantity < 0 and self.options['opening_voucher']:
                raise ImportError(f'{label}: negative opening stock cannot be received. Correct it or choose no opening voucher.')
            values = {'sku': sku, 'name': row['name'], 'description': row.get('description') or row['name'],
                      'wood': row.get('attribute', ''), 'size': row.get('size', ''), 'upc': row.get('upc', ''),
                      'alu': row.get('alu', ''), 'price': price, 'department': department, 'primary_vendor': vendor,
                      'vendor': vendor.company if vendor else '', 'tax_category': category.name, 'tax_rate': rate,
                      'unit_of_measure': row.get('unit_of_measure', ''), 'item_type': 'noninventory' if noninventory else 'inventory'}
            is_new = owned is None
            if is_new:
                values.update(on_hand=quantity, average_cost=cost, order_cost=order_cost)
            else:
                self.warn('Re-import preserves current stock and costs; opening inventory is applied once per source row.')
            item, created = self.save(Item, sid, values, 'items')
            existing[item_key(row)] = item
            if created and quantity and not noninventory:
                if self.options['opening_voucher']:
                    self.opening[department.pk].append((item, quantity, cost, sid))
                self.summary['opening_value'] = str(Decimal(self.summary['opening_value']) + money(quantity * cost))

    def vouchers(self):
        for department_id, entries in self.opening.items():
            department = next(dep for dep in self.departments.values() if dep.pk == department_id)
            now = timezone.now()
            # The item was initialized to the snapshot once above. These movements
            # explain exactly that snapshot; do not increment the stock again.
            document, _ = self.save(PurchaseDocument, entries[0][3] + ':opening', {
                'kind': 'voucher', 'status': 'posted', 'vendor': None,
                'vendor_name': 'Opening inventory', 'notes': f'Opening inventory — {department.name}',
                'invoice_date': timezone.localdate(now), 'posted_at': now,
                'total': sum((money(qty * cost) for _, qty, cost, _ in entries), ZERO)})
            for position, (item, qty, cost, sid) in enumerate(entries, 1):
                self.child(PurchaseLine, document=document, source_id=sid + ':opening', position=position,
                           item=item, sku=item.sku, item_name=item.name, description=item.description,
                           quantity=qty, cost=cost, received=qty)
                self.child(ReceivingMovement, document=document, item=item, quantity=qty, value=money(qty * cost))
            self.summary['opening_vouchers'] += 1

    def customers(self):
        file = self.payload.get('customers')
        if not file:
            return
        existing = {customer_key(c.full_name, c.phone): c
                    for c in Customer.objects.only('pk', 'source_id', 'full_name', 'phone')}
        for row in file['rows']:
            name = customer_name(row)
            if not name:
                raise ImportError(f'customers row {row["_row"]}: a name or company is required.')
            sid = source(file, row)
            owned = Customer.objects.select_for_update().filter(source_id=sid).first()
            match = existing.get(customer_key(name, row.get('phone', '')))
            if not owned and match and match.source_id != sid:
                self.duplicate('customers', row, f'Name + phone matches customer {match.full_name}; skipped.')
                continue
            exempt = boolean(row.get('tax_exempt'))
            customer, _ = self.save(Customer, sid, self.contact(row) | {
                **{field: row.get(field, '') for field in ('first_name', 'last_name', 'customer_number', 'mobile', 'notes')},
                'full_name': name, 'tax_exempt': exempt, 'tax_location': tax_location_for(exempt),
                'source_data': row, 'source_sha256': file['sha256']}, 'customers')
            existing[customer_key(name, row.get('phone', ''))] = customer

    def sales(self):
        file = self.payload.get('sales')
        if not file:
            return
        grouped = defaultdict(list)
        for row in file['rows']:
            if not row.get('receipt'):
                raise ImportError(f'sales row {row["_row"]}: Receipt # is required. Remove subtotals and report totals.')
            grouped[row['receipt']].append(row)
        for receipt, rows in grouped.items():
            sid = source(file, rows[0])
            if Document.objects.filter(source_id=sid).exists():
                self.bump('sales', 'reused')
                continue
            if not receipt.isdigit() or not 0 < int(receipt) <= 2147483647:
                raise ImportError(f'Receipt # {receipt!r} must be a positive whole number.')
            if Document.objects.filter(kind='sale').filter(Q(source_number=int(receipt)) | Q(number_value=int(receipt))).exists():
                self.duplicate('sales', rows[0], f'Receipt number {receipt} already exists; skipped.')
                continue
            dates = {sale_date(row['date']) for row in rows}
            names = {row.get('customer', '') for row in rows}
            if len(dates) != 1 or len(names) != 1:
                raise ImportError(f'Receipt {receipt}: inconsistent date/customer, or receipt numbers from multiple stores. Split the files by store.')
            subtotal, tax_total, parsed = ZERO, ZERO, []
            for row in rows:
                label = f'sales row {row["_row"]}'
                qty = number(row.get('quantity'), label + ' Qty', 3)
                price = number(row.get('price'), label + ' Price', 4, digits=16)
                extension = number(row.get('extension'), label + ' Ext Price')
                line_tax = number(row.get('tax'), label + ' Tax')
                if not row.get('item'):
                    raise ImportError(f'{label}: Item is required.')
                subtotal += extension
                tax_total += line_tax
                if row.get('tax_rate'):
                    rate = number(row['tax_rate'], label + ' Tax Rate', 3, signed=False, digits=6)
                elif extension and line_tax:
                    rate = (line_tax * 100 / extension).quantize(Decimal('0.001'))
                    self.warn('Historical line rates are inferred from exported line tax / extension where Tax Rate is absent. Zero-tax lines are treated as non-taxable; historical tax locations use the current default/exempt location. Review these limitations before using historical tax reports.')
                else:
                    rate = ZERO
                if rate < 0 or rate > 100:
                    raise ImportError(f'{label}: tax rate cannot be reconstructed (must be between 0 and 100).')
                parsed.append((row, qty, price, extension, rate))
            total = subtotal + tax_total
            number(str(total), f'Receipt {receipt} total')
            day = next(iter(dates))
            completed = timezone.make_aware(datetime.combine(day, time(12)))
            name = next(iter(names))
            matches = list(Customer.objects.filter(full_name__iexact=name)[:2]) if name else []
            tenders = self.tenders(rows, total)
            notes = 'Imported Sales History report. Tax and extensions are exported amounts; historical stock is not replayed.'
            if not tenders:
                notes += ' Totals only: tender amounts cannot be reconstructed; payment-based reports are incomplete.'
            document, _ = self.save(Document, sid, {
                'kind': 'sale', 'status': 'completed', 'source_number': int(receipt),
                'receipt_type': 1 if total < 0 else 0, 'was_completed': True,
                'customer': matches[0] if len(matches) == 1 else None,
                'customer_name': name, 'subtotal': subtotal, 'tax': tax_total, 'total': total,
                'completed_at': completed, 'tax_exempt': tax_total == 0,
                'tax_location': tax_location_for(tax_total == 0),
                'notes': notes}, 'sales')
            # auto_now_add is appropriate for native sales; imported history
            # uses its report date for both timestamps, as the backup loader does.
            Document.objects.filter(pk=document.pk).update(created_at=completed, updated_at=completed)
            document.refresh_from_db()
            self.changes[-1]['after'] = snapshot(document)
            for position, (row, qty, price, extension, rate) in enumerate(parsed, 1):
                candidates = Item.objects.filter(name__iexact=row['item'])
                if row.get('attribute'):
                    candidates = candidates.filter(wood__iexact=row['attribute'])
                if row.get('size'):
                    candidates = candidates.filter(size__iexact=row['size'])
                matches = list(candidates[:2])
                item = matches[0] if len(matches) == 1 else None
                self.child(Line, document=document, source_id=source(file, row), position=position,
                           item=item, sku=item.sku if item else '', item_name=row['item'], description=row['item'],
                           quantity=qty, price=price, recorded_amount=extension, tax_rate=rate, unit_cost=None)
            if tenders:
                for position, (method, amount) in enumerate(tenders, 1):
                    self.child(Tender, document=document, source_id=sid + f':t{position}', position=position,
                               method=method, amount=amount)
                self.summary['receipts_with_tenders'] += 1
            else:
                # No invented cash payment or account activity. The existing
                # imported document view shows the stored receipt totals.
                self.summary['receipts_totals_only'] += 1
                self.warn('Sales history includes totals-only receipts: no tender rows were created. Daily Totals, Sales Tax, customer balances and accounting exchange cannot reconstruct their payments.')

    def tenders(self, rows, total):
        methods = {'cash': 'cash', 'check': 'check', 'cheque': 'check', 'credit': 'credit', 'credit card': 'credit',
                   'debit': 'debit', 'debit card': 'debit', 'other': 'other'}
        # Tender labels alone never establish how much was paid. Repeated
        # receipt-level amounts on every item row are accepted only when identical
        # and equal to the receipt total. Split rows require explicit amounts
        # whose sum reconciles; account/gift flows need more than this report.
        entries = []
        for row in rows:
            if not row.get('tender_amount'):
                if row.get('tender'):
                    return []
                continue
            method = methods.get(key(row.get('tender')))
            if method is None:
                return []
            amount = number(row['tender_amount'], f'sales row {row["_row"]} Tender Amount')
            entries.append((method, amount))
        if not entries:
            return []
        if len(set(entries)) == 1 and entries[0][1] == total:
            return [entries[0]]
        return entries if sum((amount for _, amount in entries), ZERO) == total else []

    def run(self):
        if not self.payload or not any(file['rows'] for file in self.payload.values()):
            raise ImportError('Choose at least one export with data rows.')
        for file in self.payload.values():
            for warning in file['warnings']:
                self.warn(warning)
        self.vendors()
        self.items()
        self.vouchers()
        self.customers()
        self.sales()
        self.summary['count_rows'] = list(self.summary['counts'].items())
        return self.summary


def normalized_options(payload, options=None):
    options = dict(options or {})
    proposals = mapping_proposals(payload, options)
    options['tax_map'] = {row['code']: row['selected'] for row in proposals['tax_codes']}
    options['department_map'] = {row['name']: row['selected'] for row in proposals['departments']}
    options.setdefault('opening_voucher', True)
    return options


def file_metadata(payload):
    return [{key: value for key, value in file.items() if key not in ('rows', 'warnings')}
            | {'row_count': len(file['rows'])} for file in payload.values()]


@transaction.atomic
def import_exports(payload, options=None, *, commit=False, log=None, user=None):
    # Serialize migrations/undos, including an empty first import, with the
    # existing singleton. Individual changed records are locked as well.
    tax()
    TaxSettings.objects.select_for_update().get()
    if log:
        log = ImportLog.objects.select_for_update().get(pk=log.pk)
        if log.status != 'preview':
            raise ImportError('This preview has already been imported. Upload again to check a re-import.')
        payload, options = log.payload, log.options
    options = normalized_options(payload, options)
    loader = Loader(payload, options)
    try:
        summary = loader.run()
    except (ValidationError, IntegrityError) as exc:
        raise ImportError(f'Export validation failed: {exc}') from exc
    if not commit:
        transaction.set_rollback(True)
        return summary
    log = log or ImportLog(created_by=user)
    log.status = 'committed'
    log.files = file_metadata(payload)
    log.options = options
    log.summary = summary
    log.changes = loader.changes
    log.payload = {}  # retain provenance/counts, not a second copy of all exports
    log.committed_at = timezone.now()
    log.save()
    return log


@transaction.atomic
def undo_import(import_id):
    tax()
    TaxSettings.objects.select_for_update().get()
    try:
        log = ImportLog.objects.select_for_update().get(pk=import_id)
    except (ImportLog.DoesNotExist, ValidationError) as exc:
        raise ImportError('Import not found.') from exc
    if log.status != 'committed':
        raise ImportError('Only a committed import can be undone, once.')
    owned = defaultdict(set)
    objects = {}
    changes = {}
    for change in log.changes:
        model = apps.get_model(change['model'])
        obj = model.objects.select_for_update().filter(pk=change['pk']).first()
        if obj is None or snapshot(obj) != change['after']:
            raise ImportError('Cannot undo: imported rows were edited or used since this import. No rows were removed.')
        owned[change['model']].add(change['pk'])
        objects[(change['model'], change['pk'])] = obj
        changes[(change['model'], change['pk'])] = change
    # Check even nullable/CASCADE relations: deleting a receipt must not clear a
    # later return's link, and deleting a customer must not remove new addresses.
    for (label, pk), obj in objects.items():
        change = changes[(label, pk)]
        for relation in obj._meta.related_objects:
            model = relation.related_model
            if change['before'] is not None and model._meta.label_lower not in ('sales.document', 'sales.line', 'sales.stockmovement'):
                continue
            query = model.objects.filter(**{relation.field.attname: pk})
            if query.exclude(pk__in=owned[model._meta.label_lower]).exists():
                raise ImportError('Cannot undo: these rows have sales, receiving, or other history outside this import. No rows were removed.')
    try:
        for change in reversed(log.changes):
            obj = objects[(change['model'], change['pk'])]
            if change['before'] is None:
                obj.delete()
            else:
                values = {field.attname: field.to_python(change['before'][field.attname])
                          for field in obj._meta.concrete_fields if not field.primary_key}
                type(obj).objects.filter(pk=obj.pk).update(**values)
    except IntegrityError as exc:
        raise ImportError('Cannot undo: imported rows have dependent history. No rows were removed.') from exc
    log.status = 'undone'
    log.undone_at = timezone.now()
    log.save(update_fields=['status', 'undone_at'])
    return log
