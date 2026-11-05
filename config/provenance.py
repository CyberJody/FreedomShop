# SPDX-License-Identifier: AGPL-3.0-or-later
"""Demo source IDs identify generated native records, never imported history."""
from django.db.models import Q


def imported_source(value):
    return value is not None and not value.startswith('demo-')


def native_source(field='source_id'):
    return Q(**{field + '__isnull': True}) | Q(**{field + '__startswith': 'demo-'})
