# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): Sales History lists the app's sales order deposits and refunds beside receipts.

QuickBooks rings every order deposit as its own receipt, so imported QB deposits already show in Sales History. Payments
taken on an order in this app are `OrderPayment` rows on the order instead; this module merges them into the same list
as their own rows (date, customer, "Deposit · Sales Order N", amount, how paid), sorted together with the receipts.

The merge is one SQL UNION of light (kind, id, sort value) rows, ordered and sliced in the database; only the rows on
the requested page are then loaded in full, so a page costs the same whether the history holds 60 or 60,000 receipts.
Nothing here touches money: 's $0 tax-adjustment rows (`adjustment=True`) are left out, and no report reads this.
"""
from datetime import date

from django.db.models import Case, CharField, DateField, DecimalField, F, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce

from customers.models import tax_location_for
from .models import Document, OrderPayment

DOC, PAYMENT = 0, 1


def tax_area(prefix=''):
    """Sales History's Tax Location sort value (blank location on older documents = from the exempt flag)."""
    from shop_settings.config import tax
    config = tax()
    location, exempt = F(prefix + 'tax_location'), prefix + 'tax_exempt'
    return Case(When(**{prefix + 'tax_location': ''}, then=Case(When(**{exempt: True}, then=Value(config.location_for(True))), default=Value(config.location_for(False)))),
                default=location, output_field=CharField())


# Sales History sort field -> the same column for an order payment row.
PAYMENT_SORT = {
    'completed_at': lambda: F('created_at'),
    'display_number': lambda: Coalesce('order__source_number', 'order__number_value', output_field=IntegerField()),
    'status': lambda: Value('completed', output_field=CharField()),
    'customer_name': lambda: F('order__customer_name'),
    'cashier': lambda: F('cashier'),
    'item_count': lambda: Value(0, output_field=IntegerField()),
    'item_quantity': lambda: Value(0, output_field=DecimalField(max_digits=12, decimal_places=3)),
    'total': lambda: F('amount'),
    'payment_method': lambda: F('method'),
    'tax_area': lambda: tax_area('order__'),
    # the Awaiting Pickup filter lists receipts only (no payments), but the UNION needs the column.
    'pickup_due': lambda: Value(PICKUP_NO_DATE, output_field=DateField()),
}

# receipts with no est. pickup date sort after every dated one.
PICKUP_NO_DATE = date(9999, 12, 31)


def order_payments(customer=None, status='completed', query='', since=None):
    """The order payments Sales History shows under its filters: money taken or refunded (never tax adjustments)."""
    if status in ('cancelled', 'pickup'):  # Awaiting Pickup lists receipts only
        return OrderPayment.objects.none()
    payments = OrderPayment.objects.filter(adjustment=False, order__kind='order')
    if customer is not None:
        payments = payments.filter(order__customer=customer)
    if since is not None:
        payments = payments.filter(created_at__gte=since)
    if query:
        match = Q(order__customer_name__icontains=query) | Q(order__customer_po__icontains=query) | Q(reference__icontains=query)
        number = query.upper().removeprefix('SO-')
        if number.isdigit() and len(number) <= 18:
            match |= Q(order__number_value=int(number)) | Q(order__source_number=int(number))
        payments = payments.filter(match)
    return payments


class PaymentRow:
    """One order payment as a Sales History row; the attributes mirror what the list reads from a Document."""
    kind = 'order'
    is_order_payment = True
    status_label = 'Completed'
    type_label = ''
    item_count = ''
    item_quantity = None
    tax = 0

    def __init__(self, payment):
        order = payment.order
        self.payment = payment
        self.pk = payment.pk
        self.token = order.token
        self.completed_at = payment.created_at
        self.customer_name = order.customer_name
        self.cashier = payment.cashier  # who took it (blank before )
        self.total = payment.amount
        self.payment_label = payment.label
        self.tax_area = order.tax_location or tax_location_for(order.tax_exempt)
        self.number_or_label = f"{'Refund' if payment.amount < 0 else 'Deposit'} · {order.numbered_label}"

    def get_status_display(self):
        return self.status_label


class MergedHistory:
    """Receipts + order payments in one sorted sequence for Paginator and the continuous-scroll batches."""

    def __init__(self, documents, payments, sort, load_documents):
        field = sort.lstrip('-')
        descending = sort.startswith('-')
        doc_sort = {'completed_at': F('completed_at'), 'tax_area': tax_area(),
                    'pickup_due': Coalesce('est_pickup_date', Value(PICKUP_NO_DATE, output_field=DateField()))}.get(field, F(field))
        if field in ('display_number',):
            doc_sort = Coalesce('source_number', 'number_value', output_field=IntegerField())
        light_docs = documents
        if field in ('item_count', 'item_quantity'):
            from django.db.models import Count, Sum
            light_docs = documents.annotate(**{field: Count('lines') if field == 'item_count' else Sum('lines__quantity')})
            doc_sort = F(field)
        docs = light_docs.order_by().annotate(h_kind=Value(DOC, output_field=IntegerField()), h_id=F('pk'), h_sort=doc_sort).values_list('h_kind', 'h_id', 'h_sort')
        pays = payments.order_by().annotate(h_kind=Value(PAYMENT, output_field=IntegerField()), h_id=F('pk'), h_sort=PAYMENT_SORT[field]()).values_list('h_kind', 'h_id', 'h_sort')
        direction = '-' if descending else ''
        # Ties keep the old list's order (pk ascending), receipts before payments.
        self.union = docs.union(pays, all=True).order_by(direction + 'h_sort', 'h_kind', 'h_id')
        self.load_documents = load_documents
        self._count = None

    def count(self):
        if self._count is None:
            self._count = self.union.count()
        return self._count

    def __len__(self):
        return self.count()

    def __getitem__(self, index):
        if not isinstance(index, slice):
            return self[index:index + 1][0]
        keys = list(self.union[index])
        doc_ids = [pk for kind, pk, _ in keys if kind == DOC]
        pay_ids = [pk for kind, pk, _ in keys if kind == PAYMENT]
        docs = {d.pk: d for d in self.load_documents(Document.objects.filter(pk__in=doc_ids))} if doc_ids else {}
        pays = {p.pk: PaymentRow(p) for p in OrderPayment.objects.filter(pk__in=pay_ids).select_related('order')} if pay_ids else {}
        return [docs[pk] if kind == DOC else pays[pk] for kind, pk, _ in keys if pk in (docs if kind == DOC else pays)]
