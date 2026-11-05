# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remaining procurement, accounting for overlap between received and picked-up units."""
from decimal import Decimal
from .models import PurchaseLine


def procurement_capacity(sales_line, editing_po=None):
    received = Decimal('0')
    outstanding_other = Decimal('0')
    current_received = Decimal('0')
    for line in PurchaseLine.objects.filter(sales_line=sales_line, document__kind='po').select_related('document'):
        received += line.received
        if editing_po and line.document_id == editing_po.pk:
            current_received += line.received
        elif line.document.status not in {'cancelled', 'closed'}:
            outstanding_other += max(line.quantity - line.received, Decimal('0'))
    return max(sales_line.quantity - max(sales_line.fulfilled, received) - outstanding_other, Decimal('0')) + current_received
