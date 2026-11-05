# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable local outbox, acknowledgements and an opt-in HTTP draft transport."""
import hashlib
import copy
import json
import os
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from django.db import transaction
from django.db.models import Min
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from purchasing.models import PurchaseDocument
from sales.models import Document, OrderPayment
from shop_settings.config import business
from shop_settings.models import Business
from . import exchange
from .models import ExchangeCorrection, ExchangeRun


def outbox_path(override=None):
    value = override or business().exchange_outbox
    if not value:
        raise ValueError('Set the exchange outbox in Business Settings or use --out DIR.')
    return Path(value)


def is_closed(day, now=None):
    local = timezone.localtime(now or timezone.now())
    return day < local.date() or (day == local.date() and local.time() >= business().closing_time)


def pending_days(now=None):
    local = timezone.localtime(now or timezone.now())
    last = local.date() if is_closed(local.date(), local) else local.date() - timedelta(days=1)
    candidates = []
    for model, field in ((Document, 'completed_at'), (OrderPayment, 'created_at'), (PurchaseDocument, 'posted_at')):
        value = model.objects.aggregate(first=Min(field))['first']
        if value:
            candidates.append(timezone.localdate(value))
    for model in (ExchangeRun, ExchangeCorrection):
        value = model.objects.aggregate(first=Min('day'))['first']
        if value:
            candidates.append(value)
    if not candidates:
        return
    exported = set(ExchangeRun.objects.filter(published_at__isnull=False).values_list('day', flat=True))
    day = min(candidates)
    while day <= last:
        if day not in exported:
            yield day
        day += timedelta(days=1)


def file_path(outbox, day):
    return outbox / f'freedomshop-exchange-{day}.json'


def write_artifact(path, payload, *, frozen, previous_draft=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        if frozen and path.read_bytes() != previous_draft:
            raise ValueError(f'Outbox file differs from retained export: {path.name}. Move the conflicting file aside first.')
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def export_day(day, outbox):
    """Commit the artifact before publishing it; a crash is repairable from retained bytes."""
    from .exchange_validation import validate_exchange
    day = exchange.parse_day(day)
    with transaction.atomic():
        Business.objects.select_for_update().get(pk=business().pk)
        run, _ = ExchangeRun.objects.get_or_create(day=day)
        previous_draft = bytes(run.payload) if not run.exported_at else None
        if not run.exported_at:
            if not is_closed(day):
                raise ValueError(f'{day} is not closed yet (Business Settings closing time).')
            result = exchange._build_day(day)
            run.base_body = copy.deepcopy(result['body'])
            exchange.include_corrections(result)
            validate_exchange(result, require_checks=False)
            run.payload = exchange.json_bytes(result)
            run.file_sha256 = hashlib.sha256(run.payload).hexdigest()
            run.generated_at = parse_datetime(result['generated_at'])
            if all(result['body']['checks'].values()):
                run.exported_at = timezone.now()
                run.status, run.error, run.failure_stage = 'pending', '', ''
            else:
                run.status, run.error, run.failure_stage = 'failed', 'Export checks failed; inspect the day and source documents.', 'export'
            run.save()
        payload = bytes(run.payload)
        if hashlib.sha256(payload).hexdigest() != run.file_sha256:
            raise ValueError(f'{day}: retained file checksum mismatch.')
    try:
        if outbox is not None:
            write_artifact(file_path(outbox, day), payload, frozen=bool(run.exported_at), previous_draft=previous_draft)
    except (ValueError, OSError) as exc:
        ExchangeRun.objects.filter(pk=run.pk).exclude(status='acknowledged').update(status='failed', error=str(exc), failure_stage='export')
        raise
    if run.exported_at:
        ExchangeRun.objects.filter(pk=run.pk).update(published_at=timezone.now())
        ExchangeRun.objects.filter(pk=run.pk, failure_stage__in=['', 'export']).exclude(status='acknowledged').update(
            status='exported', error='', failure_stage='')
    run.refresh_from_db()
    return run


def collect_acks(outbox):
    failures = []
    for path in sorted(outbox.glob('*.ack.json')):
        run = None
        try:
            day = exchange.parse_day(path.name.removesuffix('.ack.json'))
            with transaction.atomic():
                run = ExchangeRun.objects.select_for_update().filter(day=day, exported_at__isnull=False).first()
                if run is None:
                    raise ValueError('No exported run for this acknowledgement day.')
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError('Acknowledgement exceeds 1 MiB.')
                raw = path.read_bytes()
                from .exchange_validation import strict_json
                ack = strict_json(raw)
                if not isinstance(ack, dict) or set(ack) != {'day', 'imported_at', 'request_keys_applied', 'errors'}:
                    raise ValueError('Expected day, imported_at, request_keys_applied and errors.')
                if ack['day'] != day.isoformat():
                    raise ValueError('Acknowledgement day does not match its filename.')
                stamp = parse_datetime(ack['imported_at']) if isinstance(ack['imported_at'], str) else None
                if stamp is None or timezone.is_naive(stamp):
                    raise ValueError('imported_at must include a time zone.')
                keys, errors = ack['request_keys_applied'], ack['errors']
                if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
                    raise ValueError('request_keys_applied must contain unique UUID strings.')
                for key in keys:
                    if str(UUID(key)) != key:
                        raise ValueError('request_keys_applied must contain canonical UUID strings.')
                if not isinstance(errors, list):
                    raise ValueError('errors must be an array.')
                commands = json.loads(bytes(run.payload))['body']['commands']
                expected = {c['request_key'] for c in commands}
                required = {c['request_key'] for c in commands if not c.get('optional')}
                if set(keys) - expected:
                    raise ValueError('Acknowledgement contains unknown request keys.')
                if errors:
                    raise ValueError('FreedomBooks: ' + json.dumps(errors, ensure_ascii=False))
                if required - set(keys):
                    raise ValueError('FreedomBooks did not apply all required request keys.')
                run.status, run.error, run.failure_stage = 'acknowledged', '', ''
                run.ack_reference = f'{path.name}#sha256={hashlib.sha256(raw).hexdigest()}'
                run.save(update_fields=['status', 'error', 'ack_reference', 'failure_stage'])
        except (ValueError, TypeError, OSError, OverflowError) as exc:
            message = f'{path.name}: {exc}'
            failures.append(message)
            if run:
                ExchangeRun.objects.filter(pk=run.pk).update(status='failed', error=message, ack_reference=path.name, failure_stage='ack')
    return failures


def validate_endpoint(endpoint):
    if not endpoint:
        return
    parsed = urlsplit(endpoint)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError('Use an HTTPS receiver URL without credentials, query parameters or a fragment.')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def push_run(run):
    """Send proposals to a receiver adapter, never directly to the Rust Command enum.

    TODO: encrypted Business.exchange_token when cryptography is already locked. No credential is
    accepted or stored. Endpoint URLs cannot embed credentials. Redirects are refused.
    """
    endpoint = business().exchange_endpoint
    if not endpoint:
        raise ValueError('Live push is disabled; set an exchange endpoint in Business Settings.')
    validate_endpoint(endpoint)
    if not run.exported_at:
        raise ValueError('Only checked, retained exports can be pushed.')
    data = json.loads(bytes(run.payload))
    failures = []
    opener = build_opener(NoRedirect)
    for command in data['body']['commands']:
        key = command['request_key']
        request = Request(endpoint, data=exchange.canonical_bytes(dict(format=exchange.FORMAT,
                          shop=data['body']['shop'], day=str(run.day), body_sha256=data['body_sha256'],
                          command=command, references=data['body']['references'])),
                          headers={'Content-Type': 'application/json', 'Idempotency-Key': key}, method='POST')
        result = dict(request_key=key, attempted_at=timezone.now().isoformat())
        try:
            with opener.open(request, timeout=15) as response:
                raw = response.read(65537)
                result.update(status=response.status, response=raw[:65536].decode('utf-8', errors='replace'))
                if len(raw) > 65536 or not 200 <= response.status < 300:
                    raise ValueError('Receiver response is too large or unsuccessful.')
                accepted = json.loads(raw)
                if not isinstance(accepted, dict) or accepted.get('request_key') != key or accepted.get('applied') is not True:
                    raise ValueError('Receiver must confirm this request_key with applied: true.')
        except HTTPError as exc:
            result.update(status=exc.code, response=exc.read(65536).decode('utf-8', errors='replace'), error='HTTP receiver rejected command.')
        except (URLError, OSError, ValueError, TypeError):
            result['error'] = 'Command was not confirmed by the receiver; retry with the same key.'
        with transaction.atomic():
            locked = ExchangeRun.objects.select_for_update().get(pk=run.pk)
            locked.push_responses.append(result)
            if 'error' in result:
                locked.status, locked.error, locked.failure_stage = 'failed', result['error'], 'push'
            locked.save(update_fields=['push_responses', 'status', 'error', 'failure_stage'])
        if 'error' in result:
            failures.append(key)
            break
    if not failures:
        ExchangeRun.objects.filter(pk=run.pk, failure_stage__in=['', 'push']).exclude(status='acknowledged').update(status='exported', error='', failure_stage='')
    return failures
