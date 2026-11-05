# SPDX-License-Identifier: AGPL-3.0-or-later
import sqlite3
import tempfile
from pathlib import Path
from django.test import TestCase
from .models import Document, Line, GiftCertificate, GiftRedemption
from .rebuild import rebuild_from_backup
from .test_rebuild import write_fixture, SID
from .views import document_data


class DiscountImportTests(TestCase):
    def test_header_reason_and_raw_line_reason_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.sqlite'
            write_fixture(path)
            with sqlite3.connect(path) as source:
                source.execute('UPDATE receipt SET DiscountLevel=? WHERE SID=?', ('Custom', SID['R1']))
                source.execute('UPDATE receipt_line SET DiscountLevel=? WHERE SID=? AND ItemPos=0', ('Price Descrepancy', SID['R1']))
            rebuild_from_backup(path, commit=True)
            from django.core.management import call_command
            import io
            Document.objects.filter(source_id=SID['R1'], kind='sale').update(discount_reason='')
            call_command('backfill_discount_reasons', str(path), stdout=io.StringIO())
            self.assertEqual(Document.objects.get(source_id=SID['R1'], kind='sale').discount_reason, '')
            call_command('backfill_discount_reasons', str(path), commit=True, stdout=io.StringIO())
        doc = Document.objects.get(source_id=SID['R1'], kind='sale')
        self.assertEqual(doc.discount_reason, 'Custom')
        self.assertEqual(doc.lines.get(position=0).discount_reason, 'Price Descrepancy')
        self.assertEqual(document_data(doc)['lines'][0]['discount_reason'], 'Price Discrepancy')
