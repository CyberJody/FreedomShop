# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restore only name columns from a reviewed backup export, preserving customer identities."""
import hashlib
import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from customers.models import Customer
from customers.names import NAME_FIELDS, composed_name, source_names


class Command(BaseCommand):
    help = 'Restore separate names from a verified original customer export; dry run unless --commit.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--expected-sha256', required=True)
        parser.add_argument('--overrides', help='Reviewed name overrides with source_id and expected_full_name.')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        raw = Path(options['file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != options['expected_sha256']:
            raise CommandError('Backup export hash does not match the reviewed file.')
        rows = json.loads(raw)
        if not isinstance(rows, list) or not rows:
            raise CommandError('Expected a nonempty list of original customer records.')
        overrides = {}
        if options.get('overrides'):
            for override in json.loads(Path(options['overrides']).read_text()):
                sid = override['source_id']
                if sid in overrides or not isinstance(override.get('expected_full_name'), str):
                    raise CommandError('Invalid or duplicate name override.')
                if any(not isinstance(override.get(field), str) for field in NAME_FIELDS):
                    raise CommandError('Overrides must supply all three name fields as text.')
                overrides[sid] = override
        prepared = {}
        for row in rows:
            sid = row.get('source_id')
            if not isinstance(sid, str) or not sid.startswith('qbpos:') or sid in prepared:
                raise CommandError('Missing or duplicate original customer identity.')
            try:
                names = source_names(row.get('source_data'))
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            if composed_name(names) != row.get('full_name'):
                raise CommandError('Original display name does not agree with source name columns.')
            prepared[sid] = (row, names)
        if overrides.keys() - prepared.keys():
            raise CommandError('Override refers to a customer outside this backup batch.')
        with transaction.atomic():
            records = Customer.objects.filter(source_id__in=prepared).order_by('pk')
            if options['commit']:
                records = records.select_for_update()
            records = list(records)
            if len(records) != len(prepared):
                raise CommandError('The batch must match existing customers; no customers will be created.')
            updates = []
            for customer in records:
                row, names = prepared[customer.source_id]
                if customer.customer_number != row.get('customer_number') or customer.source_data != row['source_data']:
                    raise CommandError(f'Source evidence differs for customer {customer.pk}; review required.')
                override = overrides.get(customer.source_id)
                desired = {field: override[field] for field in NAME_FIELDS} if override else names
                desired_full = composed_name(desired)
                if all(getattr(customer, field) == desired[field] for field in NAME_FIELDS) and customer.full_name == desired_full:
                    continue
                expected_full = override['expected_full_name'] if override else row['full_name']
                if customer.full_name != expected_full or any(getattr(customer, field) for field in NAME_FIELDS):
                    raise CommandError(f'Name was edited for customer {customer.pk}; reviewed override required.')
                for field in NAME_FIELDS:
                    setattr(customer, field, desired[field])
                customer.full_name = desired_full
                customer.version += 1
                try:
                    customer.full_clean(validate_unique=False, validate_constraints=False)
                except ValidationError as exc:
                    raise CommandError(f'Invalid restored name for customer {customer.pk}: {exc}') from exc
                updates.append(customer)
            if options['commit']:
                Customer.objects.bulk_update(updates, [*NAME_FIELDS, 'full_name', 'version'], batch_size=500)
        verb = 'Restored' if options['commit'] else 'Would restore'
        self.stdout.write(f'{verb} {len(updates)} customers; {len(records) - len(updates)} already match. '
                          'Customer IDs, contact details, source evidence, and transaction links preserved.')
