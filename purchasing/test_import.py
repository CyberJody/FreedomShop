# SPDX-License-Identifier: AGPL-3.0-or-later
import io
import json
import tempfile
from pathlib import Path
from django.core.management import call_command, CommandError
from django.test import TestCase
from purchasing.models import Vendor, Department


class LegacyMasterImportTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'masters.json'
        self.backup = 'a' * 64

    def row(self, sid, **fields):
        return dict(source_id=f'qbpos:{sid:016x}', source_data={'backup_sha256':self.backup,'values':{'SID':f'{sid:016x}'}}, active=True, **fields)

    def run_import(self, departments, vendors=(), commit=False):
        self.file.write_text(json.dumps(dict(backup_sha256=self.backup, departments=departments, vendors=list(vendors))))
        out=io.StringIO()
        call_command('import_legacy_masters',str(self.file),commit=commit,stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_idempotence_blank_codes_and_preserve_local_edits(self):
        rows=[self.row(1,code='',name='First'),self.row(2,code='',name='Second')]
        self.run_import(rows)
        self.assertFalse(Department.objects.exists())
        self.run_import(rows,commit=True)
        record=Department.objects.get(source_id=rows[0]['source_id'])
        record.name='Local edit';record.save()
        report=self.run_import(rows,commit=True)
        self.assertEqual(report['departments']['already_imported'],2)
        self.assertEqual(Department.objects.count(),2)
        record.refresh_from_db();self.assertEqual(record.name,'Local edit')
        self.assertEqual(record.source_data,rows[0]['source_data'])

    def test_invalid_later_record_rolls_back_and_preserves_local_code(self):
        Department.objects.create(code='LOCAL',name='Existing')
        rows=[self.row(1,code='A',name='First'),self.row(2,code='LOCAL',name='Conflicting')]
        with self.assertRaises(CommandError):self.run_import(rows,commit=True)
        self.assertEqual(Department.objects.count(),1)
        rows=[self.row(1,code='A',name='First'),self.row(1,code='B',name='Duplicate SID')]
        with self.assertRaises(CommandError):self.run_import(rows,commit=True)
        self.assertEqual(Department.objects.count(),1)

    def test_inactive_vendor_and_postal_leading_zero_preserved(self):
        row=self.row(3,code='V',company='Vendor',phone='123',email='',street='',city='',state='',postal_code='01234',terms='')
        row['active']=False
        self.run_import([],vendors=[row],commit=True)
        vendor=Vendor.objects.get()
        self.assertFalse(vendor.active);self.assertEqual(vendor.postal_code,'01234')
        self.assertNotIn('source_data',self.client.get(f'/purchasing/vendors/{vendor.pk}/').context['form'].fields)
