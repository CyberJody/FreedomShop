# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import a validated, normalized JSON customer export, atomically and idempotently."""
import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from customers.models import Customer
from customers.source_details import source_details, TEXT_COLUMNS, BOOL_COLUMNS
from customers.names import NAME_FIELDS, composed_name, source_names

FIELDS = ('customer_number', 'full_name', *NAME_FIELDS, 'company', 'phone', 'mobile', 'email',
          'street', 'city', 'state', 'postal_code')


class Command(BaseCommand):
    help = 'Validate and import normalized customer JSON; defaults to a dry run.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        raw = Path(options['file']).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        rows = json.loads(raw)
        if not isinstance(rows, list) or not rows:
            raise CommandError('Expected a nonempty list of normalized customer records.')
        seen = set()
        prepared = []
        for index, row in enumerate(rows):
            sid = row.get('source_id')
            if not isinstance(sid, str) or not sid.strip() or sid in seen:
                raise CommandError(f'Missing or duplicate source_id in record {index + 1}.')
            seen.add(sid)
            values = {field: row.get(field, '') for field in FIELDS}
            if any(field in row for field in NAME_FIELDS):
                if not all(field in row for field in ('first_name', 'last_name')):
                    raise CommandError(f'Provide both first_name and last_name in record {index + 1}.')
            elif isinstance(row.get('source_data'), dict) and row['source_data'].get('table') == 'Customer':
                try:
                    values.update(source_names(row['source_data']))
                except ValueError as exc:
                    raise CommandError(f'Record {index + 1}: {exc}') from exc
            if any(not isinstance(value, str) for value in values.values()):
                raise CommandError(f'Text fields must remain strings in record {index + 1}.')
            if any(values[field] for field in NAME_FIELDS):
                name = composed_name(values)
                if values['full_name'] and values['full_name'] != name:
                    raise CommandError(f'Full name conflicts with separate names in record {index + 1}.')
                values['full_name'] = name
            active = row.get('active', True)
            if not isinstance(active, bool):
                raise CommandError(f'Invalid active flag in record {index + 1}.')
            extras=source_details(row.get('source_data',{}))
            obj = Customer(**extras, source_id=sid, active=active, source_sha256=digest,
                           source_data=row.get('source_data', row), **values)
            obj.full_clean(validate_unique=False, validate_constraints=False)
            prepared.append(obj)
        existing = Customer.objects.filter(source_id__in=seen).count()
        protected = Customer.objects.filter(source_id__in=seen, locally_edited_at__isnull=False).count()
        if not options['commit']:
            self.stdout.write(f'Validated {len(rows)} records: {len(rows)-existing} new, {existing} existing; {protected} locally edited records will be preserved. No changes made.')
            return
        with transaction.atomic():
            current = {obj.source_id: obj for obj in Customer.objects.select_for_update().filter(source_id__in=seen)}
            new, updates = [], []
            protected = 0
            for obj in prepared:
                old = current.get(obj.source_id)
                if old is None:
                    new.append(obj)
                elif old.locally_edited_at is not None:
                    protected += 1
                else:
                    if not any(getattr(obj, field) for field in NAME_FIELDS) and old.composed_name:
                        if obj.full_name != old.full_name:
                            raise CommandError('Updated names require separate first and last name fields.')
                        for field in NAME_FIELDS:
                            setattr(obj, field, getattr(old, field))
                    for field in (*TEXT_COLUMNS, *BOOL_COLUMNS):
                        if field not in source_details(obj.source_data): setattr(obj,field,getattr(old,field))
                    obj.pk = old.pk
                    obj.version = old.version + 1
                    obj.imported_at = timezone.now()
                    updates.append(obj)
            Customer.objects.bulk_create(new, batch_size=500)
            Customer.objects.bulk_update(updates, [*FIELDS, *TEXT_COLUMNS, *BOOL_COLUMNS, 'active', 'source_data', 'source_sha256', 'imported_at', 'version'], batch_size=500)
        self.stdout.write(self.style.SUCCESS(f'Imported {len(new)} new and updated {len(updates)} customers; preserved {protected} locally edited records for reconciliation. Total {Customer.objects.count()}. SHA256 {digest}'))
