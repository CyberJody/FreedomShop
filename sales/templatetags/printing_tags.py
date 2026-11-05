# SPDX-License-Identifier: AGPL-3.0-or-later
from django import template

from sales.models import Printer, plain as plain_decimal

register = template.Library()


@register.simple_tag
def active_printers(kind):
    """Receipt printers for sales receipts, Letter printers for sales orders."""
    return Printer.objects.filter(active=True, kind='letter' if kind == 'order' else 'receipt')


@register.inclusion_tag('printers/print-control.html', takes_context=True)
def print_control(context, source, token, kind):
    """Printer picker (suited printers + Create PDF) and Print button, remembering the choice for this session."""
    from sales.print_documents import PDF, remembered, targets
    groups = targets(kind)
    choices = [str(p.pk) for _, printers in groups for p in printers] + [PDF]
    selected = remembered(context['request'], kind)
    if selected not in choices:
        selected = choices[0]
    return dict(groups=groups, selected=selected, kind=kind, pdf=PDF,
                print_url=f'/print/{source}/{token}/', pdf_url=f'/print/{source}/{token}/pdf/',
                label='Print Sales Order' if kind == 'order' else 'Print Receipt',
                csrf_token=context.get('csrf_token'))


@register.filter
def plain(value):
    """Two decimals unless the stored value carries more (imported QB prices and costs)."""
    return plain_decimal(value)
