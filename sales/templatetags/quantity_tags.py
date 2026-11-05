# SPDX-License-Identifier: AGPL-3.0-or-later
"""`qty` filter: quantities/counts without needless decimals. Registered as a template builtin in settings."""
from decimal import Decimal, InvalidOperation

from django import template

from sales.models import quantity_text

register = template.Library()


@register.filter
def qty(value):
    """5.000 -> '5', 60.770 -> '60.77'; None -> ''; non-numeric text (e.g. 'Unverified') passes through unchanged."""
    if value is None or value == '':
        return ''
    try:
        return quantity_text(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        return value
