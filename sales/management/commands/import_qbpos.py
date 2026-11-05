# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import only files exported by QuickBooks POS; preview by default."""
import json

from django.core.management.base import BaseCommand, CommandError

from sales.qbpos_files import ImportError, read_path
from sales.qbpos_import import import_exports, undo_import


class Command(BaseCommand):
    help = 'Preview QuickBooks POS CSV/TXT/XLSX exports; --commit imports after validating the dry run.'

    def add_arguments(self, parser):
        for kind in ('items', 'customers', 'vendors', 'sales'):
            parser.add_argument('--' + kind, metavar='FILE')
        parser.add_argument('--commit', action='store_true')
        parser.add_argument('--no-opening-voucher', action='store_true', help='Set initial on_hand without receiving vouchers.')
        parser.add_argument('--tax-map', action='append', default=[], metavar='CODE=CATEGORY', help='Tax code to TaxCategory name; repeat as needed.')
        parser.add_argument('--department-map', action='append', default=[], metavar='NAME=ID', help='Reuse an existing department; otherwise propose by name or create.')
        parser.add_argument('--undo', metavar='IMPORT_ID', help='Undo a committed batch with no subsequent changes/history (requires --commit).')

    def handle(self, *args, **options):
        try:
            if options['undo']:
                if any(options[kind] for kind in ('items', 'customers', 'vendors', 'sales')):
                    raise ImportError('--undo cannot be combined with export files.')
                if not options['commit']:
                    raise ImportError('Undo changes data. Use --undo IMPORT_ID --commit.')
                log = undo_import(options['undo'])
                self.stdout.write(f'Undone import {log.pk}')
                return
            payload = {kind: read_path(kind, options[kind]) for kind in ('items', 'customers', 'vendors', 'sales') if options[kind]}
            mapping = {'tax_map': {}, 'department_map': {}, 'opening_voucher': not options['no_opening_voucher']}
            from shop_settings.models import TaxCategory
            for value in options['tax_map']:
                code, separator, category_name = value.partition('=')
                category = TaxCategory.objects.filter(name=category_name, active=True).first()
                if not separator or category is None:
                    raise ImportError('--tax-map must be CODE=an active TaxCategory name.')
                mapping['tax_map'][code] = category.pk
            for value in options['department_map']:
                name, separator, pk = value.partition('=')
                if not separator or not pk.isdigit():
                    raise ImportError('--department-map must be NAME=department ID.')
                mapping['department_map'][name] = pk
            # --commit never bypasses the preview validation path.
            summary = import_exports(payload, mapping)
            self.stdout.write('Dry run — no business rows saved.\n' + json.dumps(summary, indent=2))
            if options['commit']:
                log = import_exports(payload, mapping, commit=True)
                self.stdout.write(f'Imported batch {log.pk}\n' + json.dumps(log.summary, indent=2))
        except (ImportError, OSError) as exc:
            raise CommandError(str(exc)) from exc
