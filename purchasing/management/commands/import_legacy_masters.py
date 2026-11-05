# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add validated legacy masters without overwriting locally edited records."""
import hashlib
import json
import re
from pathlib import Path
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from django.utils import timezone
from purchasing.models import Department, Vendor

SPECS = {
    'departments': (Department, ('code', 'name')),
    'vendors': (Vendor, ('code', 'company', 'phone', 'email', 'street', 'city', 'state', 'postal_code', 'terms')),
}


class Command(BaseCommand):
    help = 'Import a normalized QBPOS master batch. Dry run unless --commit; existing source IDs are preserved.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        raw = Path(options['file']).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw)
            assert isinstance(payload, dict)
            assert re.fullmatch('[a-f0-9]{64}', payload['backup_sha256'])
        except (ValueError, AssertionError, KeyError, TypeError):
            raise CommandError('Expected a normalized master batch with a source backup SHA256.')
        with transaction.atomic():
            prepared = []
            summary = {}
            for section, (model, fields) in SPECS.items():
                records = payload.get(section, [])
                if not isinstance(records, list):
                    raise CommandError(f'{section} must be a list.')
                seen = set()
                codes = set()
                existing = {obj.source_id: obj for obj in model.objects.select_for_update().exclude(source_id=None)}
                counts = {'new': 0, 'already_imported': 0, 'source_changed_preserved': 0}
                for row in records:
                    try:
                        sid = row['source_id']
                        assert isinstance(sid, str) and re.fullmatch('qbpos:[a-f0-9]{16}', sid) and sid not in seen
                        seen.add(sid)
                        assert type(row['active']) is bool
                        source = row['source_data']
                        assert isinstance(source, dict) and source['backup_sha256'] == payload['backup_sha256']
                        assert all(isinstance(row[field], str) for field in fields)
                        obj = model(source_id=sid, source_data=source, source_sha256=digest,
                                    imported_at=timezone.now(), active=row['active'],
                                    **{field: row[field] for field in fields})
                        obj.clean_fields()
                    except (AssertionError, KeyError, TypeError, ValidationError) as exc:
                        raise CommandError(f'Invalid {section} record {len(seen)}: {exc}') from exc
                    if sid in existing:
                        key = 'already_imported' if existing[sid].source_data == source else 'source_changed_preserved'
                        counts[key] += 1
                        continue
                    try:
                        obj.validate_constraints()
                        if obj.code and obj.code in codes:
                            raise ValidationError('Duplicate nonblank code in batch.')
                    except ValidationError as exc:
                        raise CommandError(f'Conflicting {section} code for source {sid}: {exc}') from exc
                    if obj.code:
                        codes.add(obj.code)
                    prepared.append(obj)
                    counts['new'] += 1
                summary[section] = counts
            if options['commit']:
                try:
                    with transaction.atomic():
                        for obj in prepared:
                            obj.save(force_insert=True)
                except IntegrityError as exc:
                    raise CommandError('Master data changed during import; no rows were imported. Run the dry run again.') from exc
        self.stdout.write(json.dumps({'committed': options['commit'], 'sha256': digest, **summary}, sort_keys=True))
