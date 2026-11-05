# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline validation of the shipped schema's vocabulary, with no runtime dependency.

This deliberately supports only the keywords used by exchange_schema.json. It is not a general
JSON Schema implementation. The tests also validate the contract with Draft202012Validator.
"""
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from .exchange import canonical_bytes

SCHEMA = json.loads(Path(__file__).with_name('exchange_schema.json').read_text())
VOCABULARY = {'$schema', '$id', '$defs', 'title', 'description', '$ref', 'type', 'properties', 'required',
              'additionalProperties', 'items', 'oneOf', 'anyOf', 'const', 'enum', 'pattern', 'format', 'minimum'}


def _validate(value, schema, path='$'):
    if schema is True:
        return
    if schema is False:
        raise ValueError(f'{path}: additional property is forbidden.')
    if set(schema) - VOCABULARY:
        raise ValueError('Unsupported keyword in the shipped exchange schema.')
    if '$ref' in schema:
        return _validate(value, SCHEMA['$defs'][schema['$ref'].removeprefix('#/$defs/')], path)
    for union in ('oneOf', 'anyOf'):
        if union in schema:
            matches = 0
            for member in schema[union]:
                try:
                    _validate(value, member, path)
                    matches += 1
                except ValueError:
                    pass
            if (union == 'oneOf' and matches != 1) or not matches:
                raise ValueError(f'{path}: no unique matching {union} shape.')
    types = schema.get('type', [])
    if isinstance(types, str):
        types = [types]
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
               'integer': type(value) is int, 'boolean': type(value) is bool, 'null': value is None}
    if types and not any(matches.get(kind, False) for kind in types):
        raise ValueError(f'{path}: expected {types}.')
    if 'const' in schema and (value != schema['const'] or type(value) is not type(schema['const'])):
        raise ValueError(f'{path}: invalid constant.')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: unknown value.')
    if 'minimum' in schema and value < schema['minimum']:
        raise ValueError(f'{path}: below minimum.')
    if isinstance(value, str):
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise ValueError(f'{path}: invalid string pattern.')
        fmt = schema.get('format')
        try:
            if fmt == 'uuid' and str(UUID(value)) != value:
                raise ValueError()
            if fmt == 'date' and (date.fromisoformat(value).isoformat() != value):
                raise ValueError()
            if fmt == 'date-time':
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})', value):
                    raise ValueError()
                if datetime.fromisoformat(value.replace('z', 'Z')).utcoffset() is None:
                    raise ValueError()
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{path}: invalid {fmt}.') from exc
    if isinstance(value, dict):
        if set(schema.get('required', [])) - value.keys():
            raise ValueError(f'{path}: missing required properties.')
        properties = schema.get('properties', {})
        for key, item in value.items():
            _validate(item, properties.get(key, schema.get('additionalProperties', True)), f'{path}.{key}')
    if isinstance(value, list) and 'items' in schema:
        for ordinal, item in enumerate(value):
            _validate(item, schema['items'], f'{path}[{ordinal}]')


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError(f'Invalid JSON constant: {value}')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def validate_exchange(value, *, require_checks=True):
    _validate(value, SCHEMA)
    body = value['body']
    if hashlib.sha256(canonical_bytes(body)).hexdigest() != value['body_sha256']:
        raise ValueError('body_sha256 does not match the canonical body.')
    keys = [command['request_key'] for command in body['commands']]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate command request keys.')
    for command in body['commands']:
        if 'request_key' in command['values'] and command['values']['request_key'] != command['request_key']:
            raise ValueError('Command and values request keys disagree.')
    if require_checks and not all(body['checks'].values()):
        raise ValueError('Export checks failed.')
    from .exchange_corrections import numeric_add
    combined = body['day_summary']
    for correction in body['corrections']:
        combined = numeric_add(combined, correction['summary_delta'])
    if combined != body['summary']:
        raise ValueError('Summary does not equal day_summary plus the correction deltas.')
    if all(body['checks'].values()):
        summary = body['summary']
        for field, column in [('gross_sales', 'gross'), ('discounts', 'discount'), ('taxable', 'taxable'),
                              ('nontaxable', 'nontaxable'), ('total', 'total')]:
            if sum((Decimal(row[column]) for row in body['ledger']), Decimal(0)) != Decimal(summary[field]):
                raise ValueError(f'Ledger does not tie to summary.{field}.')
        if sum((Decimal(row['tax']) for row in body['ledger']), Decimal(0)) != sum(map(Decimal, summary['tax_collected'].values())):
            raise ValueError('Ledger tax does not tie to summary.tax_collected.')
        if Decimal(summary['total']) + Decimal(summary['unlinked_deposits']) != Decimal(summary['received']) + Decimal(summary['tenders']['gift']):
            raise ValueError('Received and fiscal totals do not tie.')
    return value
