# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store QuickBooks' return -> original receipt link (receipt item DocRefSID) on already-imported lines, without a full
rebuild. Dry run unless --commit.

Reuses the importer's own `Loader.return_link`, so a backfilled database and a fresh rebuild store the same links.
"""
import json
import sqlite3

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from sales.models import Line
from sales.rebuild import Counts, Loader


class Command(BaseCommand):
    help = "Fill Line.returned_from_receipt from current.sqlite (and held.sqlite) by line source id."

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='+', help='current.sqlite, optionally held.sqlite')
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, **options):
        result = {}
        with transaction.atomic():
            for path in options['paths']:
                try:
                    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
                    con.execute('SELECT SID, ItemPos, DocRefSID FROM receipt_line LIMIT 1')
                except sqlite3.Error as exc:
                    raise CommandError(f'Cannot read receipt lines from {path}: {exc}') from exc
                counts = Counts()
                loader = Loader(con, counts, None)
                wanted = {}
                for sid, pos, ref in con.execute("SELECT SID, ItemPos, DocRefSID FROM receipt_line WHERE DocRefSID IS NOT NULL AND DocRefSID != ''"):
                    pk = loader.return_link({'DocRefSID': ref})
                    if pk:
                        wanted[f'{sid}:{pos}'] = pk
                rows = list(Line.objects.filter(source_id__in=list(wanted), document__kind='sale').only('pk', 'source_id', 'returned_from_receipt'))
                changed = [row for row in rows if row.returned_from_receipt_id != wanted[row.source_id]]
                for row in changed:
                    row.returned_from_receipt_id = wanted[row.source_id]
                Line.objects.bulk_update(changed, ['returned_from_receipt'], batch_size=2000)
                result[path.rsplit('/', 1)[-1]] = dict(qb_lines_with_link=len(wanted), matched_lines=len(rows), updated=len(changed),
                                                       link_missing=counts.get('receipt_lines_return_link_missing', 0))
                con.close()
            if not options['commit']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps(dict(committed=options['commit'], counts=result), indent=1))
