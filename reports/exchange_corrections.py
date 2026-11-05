# SPDX-License-Identifier: AGPL-3.0-or-later
"""Append-only corrections. The POS ledger remains the authority and is never adjusted here."""
import copy
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from shop_settings.config import business
from shop_settings.models import Business
from . import exchange
from .models import ExchangeCorrection, ExchangeRun


def numeric_delta(after, before):
    if isinstance(after, dict) or isinstance(before, dict):
        after, before = after or {}, before or {}
        return {key: numeric_delta(after.get(key), before.get(key)) for key in sorted(after.keys() | before.keys())}
    if isinstance(after, int) or isinstance(before, int):
        return (after or 0) - (before or 0)
    return exchange.amount(Decimal(after or '0.00') - Decimal(before or '0.00'))


def numeric_add(left, right):
    if isinstance(left, dict) or isinstance(right, dict):
        left, right = left or {}, right or {}
        return {key: numeric_add(left.get(key), right.get(key)) for key in sorted(left.keys() | right.keys())}
    if isinstance(left, int) or isinstance(right, int):
        return (left or 0) + (right or 0)
    return exchange.amount(Decimal(left or '0.00') + Decimal(right or '0.00'))


INVERSE = {
    'ReviewCustomerReceipt': ('ReverseCustomerReceipt', 'receipt_id'),
    'ReviewCustomerDeposit': ('VoidCustomerDeposit', 'record_id'),
    'ReviewDeposit': ('ReverseDeposit', 'deposit_id'),
    'ReviewCreditMemo': ('VoidCreditMemo', 'record_id'),
    'ReviewCustomerRefund': ('VoidCustomerRefund', 'record_id'),
    'ReviewDepositApplication': ('UnapplyDeposit', 'record_id'),
    'SaveJournal': ('ReverseJournal', 'journal_id'),
    'SaveBill': ('VoidBill', 'bill_id'),
}


def command_set(correction, previous_commands):
    """Reverse changed proposals and their dependents, then replace under correction-day keys.

    Daily receipts aggregate several POS receipts: reversing just one source would reverse the wrong
    amount. The importer must bind keys/revisions and reverse dependents before their sources.
    """
    day = correction.day.isoformat()
    p = exchange.Proposals(business().exchange_id, day)
    prefix = f'correction:{correction.pk}'
    before = {c['request_key']: c for c in correction.before_body['commands']}
    after = {c['request_key']: c for c in correction.after_body['commands']}
    effective = {raw['request_key']: command for raw, command in zip(before.values(), previous_commands, strict=True)}
    matched = {}  # New raw keys -> old raw keys; ordinals can shift when a group disappears.

    def semantic(value, aliases):
        if isinstance(value, dict):
            return {key: semantic(item, aliases) for key, item in value.items() if key != 'request_key'}
        if isinstance(value, list):
            return [semantic(item, aliases) for item in value]
        return aliases.get(value, value) if isinstance(value, str) else value

    while True:
        added = False
        for key, entry in after.items():
            if key in matched:
                continue
            for old_key, old in before.items():
                if old_key not in matched.values() and semantic(entry, matched) == semantic(old, {}):
                    matched[key] = old_key
                    added = True
                    break
        if not added:
            break

    def references_changed(value, changed):
        if isinstance(value, dict):
            return any(references_changed(item, changed) for key, item in value.items() if key != 'request_key')
        if isinstance(value, list):
            return any(references_changed(item, changed) for item in value)
        return isinstance(value, str) and value in changed

    while True:
        changed_before, changed_after = before.keys() - set(matched.values()), after.keys() - matched.keys()
        dependent = [key for key, old_key in matched.items() if references_changed(before[old_key], changed_before)
                     or references_changed(after[key], changed_after)]
        if not dependent:
            break
        for key in dependent:
            del matched[key]
    for raw in reversed(list(before.values())):
        if raw['request_key'] not in changed_before:
            continue
        old = effective[raw['request_key']]
        if old['type'] == 'SaveDirectoryRecord':
            continue
        inverse, identifier = INVERSE[old['type']]
        values = {identifier: old['request_key'], 'date': day,
                  'reason': f'{correction.source}; replaces {correction.source_run.day}'}
        if inverse != 'ReverseJournal':
            # Local binding placeholder, never an assumed destination-book revision.
            values['expected_revision'] = 0
        if inverse == 'ReverseCustomerReceipt':
            values['sales_receipt'] = old['sales_receipt']
        if inverse == 'VoidCustomerRefund':
            values['deposit'] = old['deposit']
        p.add(prefix + ':reverse', inverse, values, [correction.source],
              target_request_key=old['request_key'], optional=old.get('optional', False),
              requires=['Resolve target_request_key to the recorded ID and current revision; skip unrecorded optional proposals.'])
    replacements = copy.deepcopy([c for key, c in after.items() if key in changed_after])
    key_map = {key: effective[old_key]['request_key'] for key, old_key in matched.items()}
    for entry in replacements:
        section = prefix + ':' + entry['section']
        ordinal = p.counts[section]
        p.counts[section] += 1
        key_map[entry['request_key']] = exchange.request_key(p.shop_id, day, section, ordinal)
        entry['section'] = section

    def rebind(value):
        if isinstance(value, dict):
            return {key: rebind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item) for item in value]
        return key_map.get(value, value) if isinstance(value, str) else value

    replacement_by_raw_key = {raw['request_key']: rebind(raw) for raw in replacements}
    for entry in replacement_by_raw_key.values():
        if 'date' in entry['values']:
            entry['values']['date'] = day
    current = [replacement_by_raw_key[key] if key in changed_after else effective[matched[key]] for key in after]
    return p.commands + list(replacement_by_raw_key.values()), current


@transaction.atomic
def capture_cancellation(document):
    """Called at the end of the POS cancellation transaction, before it commits.

    Scan exported ledger days because removing an account sale can also change later allocations.
    Serialise with exporters through the Business row; no file or network I/O in checkout.
    """
    shop = business()
    Business.objects.select_for_update().get(pk=shop.pk)
    now = timezone.now()
    day = timezone.localdate(now)
    if ExchangeRun.objects.filter(day=day, exported_at__isnull=False).exists():
        raise ValueError("Today's exchange is already closed and exported; cancellation cannot change a closed day.")
    first_day = timezone.localdate(document.completed_at or document.created_at)
    for run in ExchangeRun.objects.filter(day__gte=first_day, day__lte=day, exported_at__isnull=False).order_by('day'):
        latest = run.corrections.order_by('-created_at', '-pk').first()
        before = latest.after_body if latest else run.base_body
        after = exchange._build_day(run.day)['body']
        # Ignore mutable directory/contact labels; financial summaries, ledger and costs decide corrections.
        if after['summary'] == before['summary'] and after['ledger'] == before['ledger']:
            continue
        correction = ExchangeCorrection.objects.create(source_run=run, day=day, created_at=now,
                     source=f'Cancellation of {document.numbered_label}', before_body=before, after_body=after)
        previous = latest.effective_commands if latest else before['commands']
        correction.commands, correction.effective_commands = command_set(correction, previous)
        correction.save(update_fields=['commands', 'effective_commands'])


def apply_corrections(body):
    rows = ExchangeCorrection.objects.filter(day=body['day']).select_related('source_run')
    body['day_summary'] = copy.deepcopy(body['summary'])
    body['corrections'] = []
    for row in rows:
        delta = numeric_delta(row.after_body['summary'], row.before_body['summary'])
        body['summary'] = numeric_add(body['summary'], delta)
        body['corrections'].append(dict(source_day=row.source_run.day.isoformat(), source=row.source,
                                       source_file_sha256=row.source_run.file_sha256, summary_delta=delta))
        body['commands'].extend(row.commands)
        body['references'].update(row.before_body['references'])
        body['references'].update(row.after_body['references'])
        for sign, historical in ((-1, row.before_body), (1, row.after_body)):
            for ledger_row in historical['ledger']:
                body['ledger'].append({**ledger_row, **{field: exchange.amount(sign * Decimal(ledger_row[field]))
                                                       for field in exchange.FIGURES}})
        for check in body['checks']:
            body['checks'][check] &= row.after_body['checks'][check] and row.before_body['checks'][check]
    return body
