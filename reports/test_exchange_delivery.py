# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import tempfile
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZipFile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register
from sales.models import Document

from .test_exchange import DAY, validator
from .test_tax_ledger import WHEN


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class ExchangeDeliveryTests(TestCase):
    def test_admin_page_download_zip_navigation_and_access(self):
        for path in ('/reports/exchange/', f'/reports/exchange/{DAY}.json', f'/reports/exchange/?start={DAY}&format=zip'):
            self.assertEqual(self.client.get(path).status_code, 302)
        sign_in_register(self.client)
        self.assertEqual(self.client.get(f'/reports/exchange/{DAY}.json').status_code, 302)
        sign_in_admin(self.client)
        page = self.client.get(f'/reports/exchange/?start={DAY}')
        self.assertContains(page, 'Generated commands')
        self.assertContains(page, 'ledger_ties_sales_tax')
        self.assertContains(self.client.get('/'), 'FreedomBooks exchange')
        self.assertContains(self.client.get('/reports/'), '/reports/exchange/')
        response = self.client.get(f'/reports/exchange/{DAY}.json')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertIn('no-store', response['Cache-Control'])
        validator(self).validate(response.json())
        zipped = self.client.get(f'/reports/exchange/?start={DAY}&end={DAY + timedelta(days=1)}&format=zip')
        self.assertEqual(zipped['Content-Type'], 'application/zip')
        with ZipFile(BytesIO(zipped.content)) as archive:
            self.assertEqual(archive.namelist(), [f'freedomshop-exchange-{DAY}.json', f'freedomshop-exchange-{DAY + timedelta(days=1)}.json'])
            for name in archive.namelist():
                validator(self).validate(json.loads(archive.read(name)))
        for query in ('start=bad', f'start={DAY}&end=2020-01-01', 'start=2020-01-01&end=2030-01-01'):
            self.assertEqual(self.client.get('/reports/exchange/?' + query).status_code, 400)
        self.assertEqual(self.client.get('/reports/exchange/2026-02-30.json').status_code, 404)
        self.assertEqual(self.client.post(f'/reports/exchange/{DAY}.json').status_code, 405)

    def test_cli_day_range_and_invalid_arguments(self):
        with tempfile.TemporaryDirectory() as output:
            out = StringIO()
            call_command('export_exchange', day=DAY.isoformat(), out=Path(output), stdout=out)
            path = Path(output) / f'freedomshop-exchange-{DAY}.json'
            first = json.loads(path.read_bytes())
            validator(self).validate(first)
            call_command('export_exchange', start=DAY.isoformat(), end=(DAY + timedelta(days=1)).isoformat(),
                         out=Path(output), stdout=out)
            second = json.loads(path.read_bytes())
            self.assertEqual(first['body'], second['body'])
            self.assertEqual(len(list(Path(output).glob('*.json'))), 2)
            for options in (dict(day=str(DAY), end=str(DAY)), dict(start=str(DAY)), dict(start='bad', end=str(DAY))):
                with self.assertRaises(CommandError):
                    call_command('export_exchange', out=Path(output), stdout=out, **options)

    def test_cli_preserves_failed_check_file_and_returns_failure(self):
        Document.objects.create(kind='sale', status='completed', total='107', subtotal='100', tax='7',
                                completed_at=WHEN, payment_breakdown=[])
        with tempfile.TemporaryDirectory() as output:
            with self.assertRaises(CommandError):
                call_command('export_exchange', day=str(DAY), out=Path(output), stdout=StringIO())
            self.assertFalse(json.loads(next(Path(output).glob('*.json')).read_bytes())['body']['checks']['tenders_equal_total'])
