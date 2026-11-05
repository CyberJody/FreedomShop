# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load QB POS held receipts (Receipt DataState 1) into the app without a full rebuild. Dry run unless --commit."""
import json

from django.core.management.base import BaseCommand, CommandError

from sales.rebuild import RebuildError, load_held_receipts


class Command(BaseCommand):
    help = ('Replace the untouched QB held receipts with those in a dataset (held.sqlite from build_held.py, or an '
            'export that keeps DataState-1 rows). Other data is not changed. Dry run unless --commit.')

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to held.sqlite or a full export with DataState-1 receipts')
        parser.add_argument('--commit', action='store_true', help='Apply the load; otherwise load and roll back.')

    def handle(self, *args, **options):
        try:
            result = load_held_receipts(options['path'], commit=options['commit'], progress=lambda m: self.stderr.write(m))
        except RebuildError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=1, sort_keys=True, default=str))
