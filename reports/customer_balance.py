# SPDX-License-Identifier: AGPL-3.0-or-later
"""Customer Balances: customers whose charge account has a balance (owed or credit), with totals.

Source is each customer's Account Balance: the QB POS balance from the backup plus charges and payments on account
recorded on receipts in this app. Whole cents. Rendered with reports.tabular_exports.
"""
from config.provenance import native_source
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import F, Max, Q

from customers.models import Customer
from sales.models import Document

from .general_sales import Column

ZERO = Decimal('0.00')
SHOW = {'owed': 'Customers who owe', 'credit': 'Customers with a credit', 'all': 'All non-zero balances'}
SORTS = {'name': 'Customer name', 'balance': 'Largest balance first', 'last_sale': 'Most recent sale first'}

COLUMNS = [
    Column('customer', 'Customer', 'text', 0.22),
    Column('phone', 'Phone', 'text', 0.10),
    Column('city', 'City', 'text', 0.12),
    Column('customer_number', 'Customer #', 'text', 0.10),
    Column('charge', 'Charge Acct', 'text', 0.07),
    Column('last_sale', 'Last Sale', 'text', 0.08),
    Column('limit', 'Account Limit', 'money', 0.10),
    Column('balance', 'Account Balance', 'money', 0.11, total=True),
]


@dataclass
class Group:
    label: str
    rows: list = field(default_factory=list)
    totals: dict = field(default_factory=lambda: {'balance': ZERO})


@dataclass
class Report:
    show: str
    sort: str
    charge_only: bool
    groups: list
    totals: dict
    customers: int
    owed: Decimal
    credit: Decimal
    as_of: object

    title = 'Customer Balances'
    columns = COLUMNS
    grouped = False
    discounted_lines = 0
    empty_message = 'No customers with a balance.'

    @property
    def lines(self):
        return self.customers

    @property
    def subtitle(self):
        parts = [SHOW[self.show]]
        if self.charge_only:
            parts.append('charge accounts only')
        parts.append(f'balances as of QuickBooks backup{f" (last receipt {self.as_of:%-m/%-d/%Y})" if self.as_of else ""}')
        return ' · '.join(parts)

    @property
    def notes(self):
        return [f'{self.customers} customer{"s" if self.customers != 1 else ""} · owed ${self.owed:,.2f} · credits ${self.credit:,.2f}']


def build(show='owed', sort='balance', charge_only=False):
    show = show if show in SHOW else 'owed'
    sort = sort if sort in SORTS else 'balance'
    filters = {'owed': Q(balance__gt=0), 'credit': Q(balance__lt=0), 'all': ~Q(balance=0)}[show]
    qs = Customer.objects.annotate(balance=F('imported_account_balance') + F('app_account_activity')).filter(filters)
    if charge_only:
        qs = qs.filter(charge_account=True)
    order = {'name': ('full_name', 'company', 'pk'),
             'balance': ('balance', 'full_name') if show == 'credit' else ('-balance', 'full_name'),
             'last_sale': ('-imported_last_sale', 'full_name')}[sort]
    if sort == 'last_sale':
        qs = qs.order_by(F('imported_last_sale').desc(nulls_last=True), 'full_name')
    else:
        qs = qs.order_by(*order)
    group = Group('All')
    owed = credit = ZERO
    for c in qs.iterator(chunk_size=2000):
        balance = c.account_balance
        group.rows.append(dict(customer=c.display_name, phone=c.phone or c.mobile, city=c.city, customer_number=c.customer_number,
                               charge='Yes' if c.charge_account else '', last_sale=f'{c.imported_last_sale:%-m/%-d/%Y}' if c.imported_last_sale else '',
                               limit=c.account_limit, balance=balance, pk=c.pk))
        group.totals['balance'] += balance
        if balance > 0:
            owed += balance
        else:
            credit += balance
    as_of = Document.objects.filter(~native_source(), kind='sale').aggregate(last=Max('completed_at'))['last']
    return Report(show, sort, charge_only, [group], dict(group.totals), len(group.rows), owed, credit, as_of)
