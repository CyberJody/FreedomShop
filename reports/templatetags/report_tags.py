# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal

from django import template

from sales.models import money, plain

register = template.Library()


@register.filter
def dollars(value):
    """$1,234.56 with thousands separators; negatives as -$12.34 (returns and reversals)."""
    value = money(value)
    return f'-${-value:,.2f}' if value < 0 else f'${value:,.2f}'


@register.filter
def price(value):
    """Unit price in whole cents, rounded half-up."""
    return dollars(value)


@register.simple_tag
def cell(row, column):
    """Formatted value of a column-driven report row (dict) for one Column spec."""
    value = row[column.key]
    if column.kind == 'money':
        return dollars(value)
    if column.kind == 'percent':
        return '' if value is None else f'{value:.2f}%'
    if value is None:
        return ''
    return value


@register.simple_tag
def total_row(report, totals, label):
    """Cells for a subtotal/grand-total row: label spanning the text columns, totals under total columns."""
    from django.utils.html import escape, format_html_join
    from django.utils.safestring import mark_safe
    columns = report.columns
    first_numeric = next((i for i, c in enumerate(columns) if c.kind in ('qty', 'money', 'int', 'percent')), len(columns))
    parts = [f'<td colspan="{max(first_numeric, 1)}">{escape(label)}</td>']
    for c in columns[max(first_numeric, 1):]:
        if c.total:
            value = totals[c.key]
            if c.kind == 'percent':
                text = '' if value is None else f'{value:.2f}%'
            else:
                text = dollars(value) if c.kind == 'money' else (f"{value.normalize():f}" if c.kind == 'qty' else value)
            parts.append(f'<td class="number">{escape(text)}</td>')
        else:
            parts.append('<td></td>')
    return mark_safe(''.join(parts))
