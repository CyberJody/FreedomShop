# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import hashlib
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin
from sales.models import Document, Line
from sales.services import WorkflowError, save_document, save_order_payment
from shop_settings.config import business
from shop_settings.forms import BusinessForm
from shop_settings.models import Business

from . import daily_totals, exchange, exchange_sync, sales_tax
from .exchange_validation import strict_json, validate_exchange
from .models import ExchangeCorrection, ExchangeRun
from .tax_periods import Period
from .test_exchange import validator
from .test_tax_ledger import LedgerFixture, WHEN

DAY = WHEN.date()
NEXT = DAY + timedelta(days=1)


class ExchangeSyncTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outbox = Path(temporary.name)
        self.shop = business()
        self.shop.exchange_outbox = str(self.outbox)
        self.shop.save()

    def export(self, day=DAY):
        return exchange_sync.export_day(day, self.outbox)

    def ack(self, run, **changes):
        keys = [c['request_key'] for c in json.loads(bytes(run.payload))['body']['commands'] if not c.get('optional')]
        value = dict(day=str(run.day), imported_at=WHEN.isoformat(), request_keys_applied=keys, errors=[])
        value.update(changes)
        path = self.outbox / f'{run.day}.ack.json'
        path.write_text(json.dumps(value))
        return path

    def cancel(self, document, when=WHEN + timedelta(days=1), **extra):
        with mock.patch('django.utils.timezone.now', return_value=when):
            return save_document(dict(action='cancel', version=document.version, **extra), document.kind, document.token,
                                 override={'reason': 'Demo correction'})

    def assert_lifetime(self, start=DAY, end=NEXT):
        bodies = [json.loads(bytes(r.payload))['body'] for r in ExchangeRun.objects.filter(day__range=(start, end))]
        period = Period('custom', start, end, 'Lifetime')
        tax, daily = sales_tax.build(period), daily_totals.build(period)
        for key, field in [('gross_sales', 'gross'), ('discounts', 'discount'), ('taxable', 'taxable'),
                           ('nontaxable', 'nontaxable'), ('total', 'total')]:
            self.assertEqual(sum((Decimal(b['summary'][key]) for b in bodies), Decimal(0)), getattr(tax, field), key)
        self.assertEqual(sum(Decimal(v) for b in bodies for v in b['summary']['tax_collected'].values()), tax.tax)
        self.assertEqual(sum(Decimal(b['summary']['received']) for b in bodies), daily.totals['received'])
        for body in bodies:
            validator(self).validate(dict(body=body, body_sha256=hashlib.sha256(exchange.canonical_bytes(body)).hexdigest(),
                                    generated_at=WHEN.isoformat()))
            self.assertTrue(all(body['checks'].values()))

    def test_pending_fills_empty_gaps_skips_open_today_and_is_idempotent(self):
        self.doc()
        now = WHEN + timedelta(days=2)
        with mock.patch('django.utils.timezone.now', return_value=now):
            call_command('export_exchange', pending=True, stdout=StringIO())
            first = {r.day: bytes(r.payload) for r in ExchangeRun.objects.all()}
            call_command('export_exchange', pending=True, stdout=StringIO())
        self.assertEqual(set(first), {DAY, NEXT})
        self.assertEqual(ExchangeRun.objects.count(), 2)
        for run in ExchangeRun.objects.all():
            self.assertEqual(bytes(run.payload), first[run.day])
            self.assertEqual(run.status, 'exported')
            self.assertEqual(run.file_sha256, hashlib.sha256(bytes(run.payload)).hexdigest())
        self.assertEqual(json.loads(first[NEXT])['body']['commands'], [])
        self.assertFalse(exchange_sync.is_closed(now.date(), now))
        with mock.patch('django.utils.timezone.now', return_value=now.replace(hour=23, minute=59)):
            call_command('export_exchange', pending=True, stdout=StringIO())
        self.assertTrue(ExchangeRun.objects.filter(day=now.date()).exists())

    def test_close_time_uses_local_zone_and_custom_time(self):
        from datetime import time
        self.shop.closing_time = time(18)
        self.shop.save()
        self.assertFalse(exchange_sync.is_closed(DAY, WHEN.replace(hour=17, minute=59)))
        self.assertTrue(exchange_sync.is_closed(DAY, WHEN.replace(hour=18)))
        self.assertFalse(exchange_sync.is_closed(NEXT, WHEN.replace(hour=23, minute=59)))
        with timezone.override('UTC'):
            self.assertTrue(exchange_sync.is_closed(DAY, WHEN.replace(hour=17, minute=59)))

    def test_no_data_pending_is_empty_and_large_backlog_is_not_range_limited(self):
        self.assertEqual(list(exchange_sync.pending_days(WHEN)), [])
        self.doc(when=WHEN - timedelta(days=400))
        self.assertEqual(len(list(exchange_sync.pending_days(WHEN))), 400)

    def test_open_day_refused_and_invalid_cli_options(self):
        with mock.patch('django.utils.timezone.now', return_value=WHEN), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(ExchangeRun.objects.exists())
        for options in ({}, {'end': str(DAY)}, {'day': str(DAY), 'end': str(DAY)}, {'start': str(DAY)}, {'push': True}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command('export_exchange', stdout=StringIO(), **options)
        self.shop.exchange_outbox = ''
        self.shop.save()
        with self.assertRaises(CommandError):
            call_command('export_exchange', pending=True, stdout=StringIO())

    def test_retained_bytes_survive_source_changes_and_outbox_loss(self):
        doc = self.doc()
        run = self.export()
        original = bytes(run.payload)
        self.shop.name = 'Changed Demo Shop'
        self.shop.save()
        Document.objects.filter(pk=doc.pk).update(total='999')
        exchange_sync.file_path(self.outbox, DAY).unlink()
        repeated = self.export()
        self.assertEqual(bytes(repeated.payload), original)
        self.assertEqual(exchange.json_bytes(exchange.build_day(DAY)), original)
        self.assertEqual(exchange_sync.file_path(self.outbox, DAY).read_bytes(), original)
        self.assertEqual(ExchangeRun.objects.count(), 1)

    def test_outbox_failure_is_retryable_without_changing_retained_bytes(self):
        self.doc()
        with mock.patch('reports.exchange_sync.write_artifact', side_effect=OSError('Demo disk failure')):
            with self.assertRaises(OSError):
                self.export()
        run = ExchangeRun.objects.get(day=DAY)
        original = bytes(run.payload)
        self.assertEqual(run.status, 'failed')
        self.assertIsNotNone(run.exported_at)
        self.assertIn(DAY, list(exchange_sync.pending_days(WHEN + timedelta(days=1))))
        self.assertEqual(bytes(self.export().payload), original)

    def test_conflicting_outbox_is_never_overwritten(self):
        run = self.export()
        path = exchange_sync.file_path(self.outbox, DAY)
        path.write_bytes(b'Demo conflicting artifact')
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(path.read_bytes(), b'Demo conflicting artifact')
        self.assertEqual(ExchangeRun.objects.get(day=DAY).file_sha256, run.file_sha256)

    def test_failed_check_file_can_be_fixed_before_first_successful_export(self):
        document = self.doc(payments=())
        first = self.export()
        self.assertEqual(first.status, 'failed')
        self.assertIsNone(first.exported_at)
        document.payment_breakdown = [{'method': 'cash', 'amount': '107.00'}]
        document.save()
        second = self.export()
        self.assertEqual(second.status, 'exported')
        self.assertNotEqual(first.file_sha256, second.file_sha256)
        self.assertEqual(ExchangeRun.objects.count(), 1)

    def test_successful_ack_and_repeat_collection(self):
        self.doc()
        run = self.export()
        path = self.ack(run)
        call_command('export_exchange', collect_acks=True, stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, 'acknowledged')
        self.assertIn(path.name, run.ack_reference)
        self.assertEqual(exchange_sync.collect_acks(self.outbox), [])
        self.assertEqual(self.export().status, 'acknowledged')

    def test_invalid_partial_and_error_acks_fail_then_valid_ack_recovers(self):
        self.doc()
        run = self.export()
        for changes in ({'day': str(NEXT)}, {'imported_at': 'bad'}, {'imported_at': '2026-09-24T12:00:00'},
                        {'request_keys_applied': []}, {'request_keys_applied': [str(uuid4())]},
                        {'request_keys_applied': ['bad']}, {'errors': ['Demo tax mapping missing']}):
            with self.subTest(changes=changes):
                self.ack(run, **changes)
                self.assertTrue(exchange_sync.collect_acks(self.outbox))
                run.refresh_from_db()
                self.assertEqual(run.status, 'failed')
        self.assertIn('Demo tax mapping missing', run.error)
        self.assertEqual(self.export().status, 'failed')  # A retry is not an import acknowledgement.
        self.ack(run)
        self.assertEqual(exchange_sync.collect_acks(self.outbox), [])
        run.refresh_from_db()
        self.assertEqual(run.status, 'acknowledged')

    def test_malformed_ack_does_not_stop_other_days(self):
        one, two = self.export(), self.export(NEXT)
        (self.outbox / f'{DAY}.ack.json').write_text('{')
        self.ack(two)
        failures = exchange_sync.collect_acks(self.outbox)
        self.assertEqual(len(failures), 1)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(one.status, 'failed')
        self.assertEqual(two.status, 'acknowledged')

    def test_duplicate_unknown_and_oversized_ack_fields_are_rejected(self):
        run = self.export()
        path = self.ack(run)
        path.write_text(path.read_text().replace('"day":', '"extra": 1, "day":'))
        self.assertTrue(exchange_sync.collect_acks(self.outbox))
        path.write_text('{"day":"2026-09-24","day":"2026-09-24"}')
        self.assertTrue(exchange_sync.collect_acks(self.outbox))
        path.write_bytes(b' ' * (1024 * 1024 + 1))
        self.assertTrue(exchange_sync.collect_acks(self.outbox))

    @override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
    def test_admin_run_list_error_escape_reexport_and_exact_download(self):
        self.doc()
        run = self.export()
        self.ack(run, errors=['<script>Demo mapping failure</script>'])
        exchange_sync.collect_acks(self.outbox)
        url = f'/reports/exchange/{DAY}/re-export/'
        self.assertEqual(self.client.post(url).status_code, 403)
        sign_in_admin(self.client)
        page = self.client.get(f'/reports/exchange/?start={DAY}')
        self.assertContains(page, 'Failed')
        self.assertContains(page, 'exchange-fail')
        self.assertContains(page, '&lt;script&gt;Demo mapping failure&lt;/script&gt;')
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, bytes(run.payload))
        self.assertEqual(self.client.get(f'/reports/exchange/{DAY}.json').content, bytes(run.payload))
        self.assertEqual(self.client.post(f'/reports/exchange/{NEXT}/re-export/').status_code, 404)

    def test_receipt_cancellation_preserves_original_and_lifetime_cent_tie(self):
        sale = self.doc()
        other = self.doc(total='53.50', tax='3.50', payments=(('cash', '53.50'),))
        first = self.export()
        original = bytes(first.payload)
        self.cancel(sale)
        self.assertEqual(ExchangeCorrection.objects.count(), 1)
        result = self.export(NEXT)
        body = json.loads(bytes(result.payload))['body']
        self.assertEqual(body['summary']['total'], '-107.00')
        self.assertEqual(body['day_summary']['total'], '0.00')
        types = {c['type'] for c in body['commands']}
        self.assertIn('ReverseCustomerReceipt', types)
        self.assertIn('ReviewCustomerReceipt', types)
        self.assertIn('correction:', body['commands'][0]['section'])
        self.assertEqual(bytes(self.export().payload), original)
        self.assert_lifetime()
        sale.refresh_from_db()
        self.assertEqual(sale.status, 'cancelled')
        other.refresh_from_db()
        self.assertEqual(other.status, 'completed')

    def test_two_cancellations_same_day_reference_replacements_and_tie(self):
        one, two = self.doc(), self.doc()
        self.export()
        self.cancel(one)
        self.cancel(two)
        self.export(NEXT)
        rows = list(ExchangeCorrection.objects.all())
        self.assertEqual(len(rows), 2)
        replacement_keys = {c['request_key'] for c in rows[0].commands if c['type'] == 'ReviewCustomerReceipt'}
        reversed_keys = {c['target_request_key'] for c in rows[1].commands if c['type'] == 'ReverseCustomerReceipt'}
        self.assertEqual(reversed_keys, replacement_keys)
        self.assert_lifetime()

    def test_correction_keeps_unaffected_tender_and_voucher_bindings(self):
        from purchasing.models import PurchaseDocument, PurchaseLine, Vendor
        one = self.doc()
        self.doc(payments=(('check', '107'),))
        vendor = Vendor.objects.create(code='DEMO', company='Demo Supplier')
        voucher = PurchaseDocument.objects.create(kind='voucher', status='posted', vendor=vendor,
                                                  posted_at=WHEN, total='40')
        PurchaseLine.objects.create(document=voucher, position=1, item_name='Demo chair', quantity=1, cost='40')
        run = self.export()
        original = json.loads(bytes(run.payload))['body']['commands']
        unaffected = {c['request_key'] for c in original if c['type'] == 'SaveBill' or
                      (c['type'] == 'ReviewCustomerReceipt' and c['values']['method'] == 'CHECK')}
        self.cancel(one)
        result = json.loads(bytes(self.export(NEXT).payload))
        targets = {c['target_request_key'] for c in result['body']['commands'] if 'target_request_key' in c}
        self.assertFalse(unaffected & targets)
        self.assertEqual(len(unaffected), 2)
        self.assert_lifetime()

    def test_cancellation_rolls_back_correction_and_preserves_frozen_day_guard(self):
        sale = self.doc()
        self.export()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.cancel(sale)
                raise RuntimeError('Demo rollback')
        sale.refresh_from_db()
        self.assertEqual(sale.status, 'completed')
        self.assertFalse(ExchangeCorrection.objects.exists())
        self.export(NEXT)
        with self.assertRaises(WorkflowError):
            self.cancel(sale)
        sale.refresh_from_db()
        self.assertEqual(sale.status, 'completed')

    def test_order_cancellation_refund_stays_on_correction_day(self):
        order = self.doc(order=True)
        with mock.patch('django.utils.timezone.now', return_value=WHEN):
            save_order_payment(order.pk, dict(amount='107', method='cash', version=order.version, token=str(uuid4())))
        order.refresh_from_db()
        self.export()
        self.cancel(order, refund_deposit=True, deposit_refund_amount='107', deposit_refund_method='cash')
        second = self.export(NEXT)
        body = json.loads(bytes(second.payload))['body']
        refunds = [c for c in body['commands'] if c['type'] == 'ReviewCustomerRefund']
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]['values']['amount'], '107.00')
        self.assertEqual(refunds[0]['next_types'], ['RecordCustomerRefund'])
        self.assert_lifetime()

    def test_deposit_receipt_cancellation_uses_void_customer_deposit(self):
        order = self.doc(order=True)
        deposit = self.doc(receipt_type=2, deposit_order=order)
        self.export()
        self.cancel(deposit)
        second = self.export(NEXT)
        body = json.loads(bytes(second.payload))['body']
        self.assertIn('VoidCustomerDeposit', {c['type'] for c in body['commands']})
        self.assert_lifetime()

    def test_native_return_after_export_uses_credit_and_refund_without_rewrite(self):
        sale = self.doc()
        original = bytes(self.export().payload)
        returned = self.doc(total='-107', tax='-7', payments=(('cash', '-107'),), when=WHEN + timedelta(days=1), lines=[])
        Line.objects.create(document=returned, position=1, item_name='ITEM', quantity=-1, price='100', tax_rate=7,
                            returned_from_line=sale.lines.get())
        second = self.export(NEXT)
        self.assertIn('ReviewCustomerRefund', {c['type'] for c in json.loads(bytes(second.payload))['body']['commands']})
        self.assertEqual(bytes(self.export().payload), original)
        self.assert_lifetime()

    def test_account_cancellation_carries_later_allocation_tax_delta_to_actual_day(self):
        sale = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_exempt=True, tax_location='Exempt')
        self.doc(total='100', tax='0', poa=True, payments=(('check', '100'),), when=WHEN + timedelta(days=1))
        first, second = self.export(), self.export(NEXT)
        original = [bytes(first.payload), bytes(second.payload)]
        day = DAY + timedelta(days=2)
        self.cancel(sale, when=WHEN + timedelta(days=2))
        corrected = self.export(day)
        value = json.loads(bytes(corrected.payload))
        validate_exchange(value)
        self.assertNotEqual(value['body']['summary']['tax_collected'], value['body']['day_summary']['tax_collected'])
        self.assertEqual([bytes(self.export().payload), bytes(self.export(NEXT).payload)], original)
        self.assert_lifetime(end=day)

    def test_cancelling_exported_return_reverses_credit_refund_and_cost(self):
        sale = self.doc()
        sale.lines.update(unit_cost='40')
        returned = self.doc(total='-107', tax='-7', payments=(('cash', '-107'),), when=WHEN + timedelta(days=1), lines=[])
        Line.objects.create(document=returned, position=1, item_name='ITEM', quantity=-1, price='100', tax_rate=7,
                            returned_from_line=sale.lines.get(), unit_cost='40')
        self.export()
        self.export(NEXT)
        day = DAY + timedelta(days=2)
        self.cancel(returned, when=WHEN + timedelta(days=2))
        correction = json.loads(bytes(self.export(day).payload))
        types = {c['type'] for c in correction['body']['commands']}
        self.assertTrue({'VoidCreditMemo', 'VoidCustomerRefund', 'ReverseJournal'} <= types)
        self.assertEqual(correction['body']['summary']['cogs'], '40.00')
        validate_exchange(correction)
        self.assert_lifetime(end=day)

    @override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
    def test_business_screen_saves_outbox_clock_and_validates_endpoint(self):
        from shop_settings.test_business import form_data
        sign_in_admin(self.client)
        response = self.client.post('/settings/business/', form_data(self.shop, exchange_outbox=str(self.outbox),
                                   closing_time='18:30', exchange_endpoint='https://example.test/exchange'))
        self.assertEqual(response.status_code, 302)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.closing_time.isoformat(), '18:30:00')
        self.assertEqual(self.shop.exchange_endpoint, 'https://example.test/exchange')
        form = BusinessForm(form_data(self.shop, exchange_outbox='relative/path'), instance=self.shop)
        self.assertFalse(form.is_valid())
        self.assertIn('exchange_outbox', form.errors)
        form = BusinessForm(form_data(self.shop, exchange_endpoint='https://user:secret@example.test'), instance=self.shop)
        self.assertFalse(form.is_valid())
        self.assertIn('exchange_endpoint', form.errors)

    def test_live_push_is_disabled_by_default_and_endpoint_cannot_contain_secrets(self):
        run = self.export()
        with mock.patch('reports.exchange_sync.build_opener') as opener:
            with self.assertRaises(ValueError):
                exchange_sync.push_run(run)
            opener.assert_not_called()
        for url in ('http://example.test/commands', 'https://user:secret@example.test/commands',
                    'https://example.test/commands?token=secret', 'https://example.test/#secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                exchange_sync.validate_endpoint(url)
        self.assertNotIn('exchange_token', {f.name for f in Business._meta.fields})
        self.assertNotIn('exchange_token', BusinessForm().fields)

    def enable_push(self):
        self.shop.exchange_endpoint = 'https://example.test/exchange/commands'
        self.shop.save()

    def mock_response(self, request, timeout):
        value = json.loads(request.data)
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(dict(request_key=value['command']['request_key'], applied=True)).encode()
        response.__enter__.return_value = response
        return response

    def test_live_push_mocked_receiver_and_retries_keep_keys(self):
        self.doc()
        run = self.export()
        self.enable_push()
        with mock.patch('reports.exchange_sync.build_opener') as opener:
            opener.return_value.open.side_effect = self.mock_response
            self.assertEqual(exchange_sync.push_run(run), [])
            first = [call.args[0] for call in opener.return_value.open.call_args_list]
            self.assertEqual(exchange_sync.push_run(run), [])
            second = [call.args[0] for call in opener.return_value.open.call_args_list[len(first):]]
        self.assertEqual([r.data for r in first], [r.data for r in second])
        for request in first:
            body = json.loads(request.data)
            self.assertEqual(request.get_header('Idempotency-key'), body['command']['request_key'])
            self.assertEqual(request.method, 'POST')
            self.assertEqual(body['day'], str(DAY))
        run.refresh_from_db()
        self.assertEqual(len(run.push_responses), 2 * len(first))
        self.assertEqual(run.status, 'exported')

    def test_live_push_failure_records_response_and_stops_dependents(self):
        self.doc()
        run = self.export()
        self.enable_push()
        error = HTTPError(self.shop.exchange_endpoint, 422, 'Demo invalid mapping', {}, BytesIO(b'{"error":"Demo tax mapping"}'))
        with mock.patch('reports.exchange_sync.build_opener') as opener:
            opener.return_value.open.side_effect = error
            self.assertTrue(exchange_sync.push_run(run))
            self.assertEqual(opener.return_value.open.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.push_responses[0]['status'], 422)
        self.assertIn('Demo tax mapping', run.push_responses[0]['response'])
        self.assertEqual(self.export().status, 'failed')

    def test_live_push_does_not_treat_unconfirmed_or_network_response_as_success(self):
        self.doc()
        run = self.export()
        self.enable_push()
        with mock.patch('reports.exchange_sync.build_opener') as opener:
            opener.return_value.open.side_effect = URLError('Demo offline')
            self.assertTrue(exchange_sync.push_run(run))
        with mock.patch('reports.exchange_sync.build_opener') as opener:
            response = mock.MagicMock(status=200)
            response.__enter__.return_value = response
            response.read.return_value = b'{"applied":true}'
            opener.return_value.open.return_value = response
            self.assertTrue(exchange_sync.push_run(run))

    def test_offline_schema_validator_checksum_keys_and_examples(self):
        self.doc()
        result = exchange.build_day(DAY)
        validate_exchange(result)
        variants = []
        bad = copy.deepcopy(result)
        bad['body']['summary']['total'] = 107.0
        variants.append(bad)
        bad = copy.deepcopy(result)
        bad['body']['day'] = '2026-02-30'
        variants.append(bad)
        bad = copy.deepcopy(result)
        bad['body_sha256'] = '0' * 64
        variants.append(bad)
        bad = copy.deepcopy(result)
        bad['extra'] = True
        variants.append(bad)
        for value in variants:
            with self.assertRaises(ValueError):
                validate_exchange(value)
        with self.assertRaises(ValueError):
            strict_json('{"day":1,"day":2}')
        paths = sorted((Path(__file__).resolve().parent.parent / 'docs/exchange-examples').glob('*.json'))
        self.assertEqual(len(paths), 3)
        for path in paths:
            value = strict_json(path.read_bytes())
            validator(self).validate(value)
            validate_exchange(value)
        call_command('validate_exchange', *paths, stdout=StringIO())
