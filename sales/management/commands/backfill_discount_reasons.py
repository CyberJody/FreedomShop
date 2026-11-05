# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill imported receipt discount reasons only. Dry run by default; no money changes."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from sales.models import Document
from sales.rebuild import receipt_discount_fields


class Command(BaseCommand):
    help = 'Backfill receipt DiscountLevel by source SID without rebuilding business data.'

    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        try:
            with closing(sqlite3.connect(Path(options['path']).resolve().as_uri() + '?mode=ro', uri=True)) as source:
                source.row_factory = sqlite3.Row
                wanted = {r['SID']: receipt_discount_fields(r)['discount_reason'] for r in source.execute("SELECT SID, DiscountLevel FROM receipt WHERE selected=1 AND DiscountLevel IS NOT NULL AND DiscountLevel != ''")}
        except sqlite3.Error as exc:
            raise CommandError(f'Cannot read receipt discount reasons: {exc}') from exc
        with transaction.atomic():
            rows = list(Document.objects.filter(kind='sale', source_id__in=wanted).only('pk','source_id','discount_reason'))
            changed = [row for row in rows if row.discount_reason != wanted[row.source_id]]
            for row in changed:
                row.discount_reason = wanted[row.source_id]
            Document.objects.bulk_update(changed, ['discount_reason'], batch_size=1000)
            if not options['commit']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps({'committed':options['commit'], 'source_reasons':len(wanted),'matched':len(rows),'updated':len(changed)}))
