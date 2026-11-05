# SPDX-License-Identifier: AGPL-3.0-or-later
"""Customer Balances report and the QB-style Customer Detail data (step 1)."""
from datetime import date, datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from customers import detail
from customers.models import Customer
from sales.models import Document, Printer, Tender
from sales.rebuild import account_fields

from . import customer_balance
from .tests import AdminModeClientTests
from accounts.signin_helpers import sign_in_register

NY = ZoneInfo('America/New_York')


def customer(sid, first, last, balance='0', **extra):
    return Customer.objects.create(source_id=sid, first_name=first, last_name=last, full_name=f'{first} {last}'.strip(),
                                   imported_account_balance=Decimal(balance), **extra)


class AccountFieldImportTests(TestCase):
    def test_qb_columns_map_and_round_to_cents(self):
        row = dict(AR=1, UseAccCharge=1, CreditLimit='500', CreditUsed='1054.739', Credit30Overdue='0', Credit60Overdue='10.005',
                   Credit90Overdue=None, LastSale='2024-07-31T00:00:00.000000')
        self.assertEqual(account_fields(row), dict(use_with_quickbooks=True, charge_account=True, account_limit=Decimal('500.00'),
                                                   imported_account_balance=Decimal('1054.74'), imported_past_due=Decimal('10.01'),
                                                   imported_last_sale=date(2024, 7, 31)))


class CustomerBalanceReportTests(AdminModeClientTests):
    @classmethod
    def setUpTestData(cls):
        cls.a = customer('A', 'ALEX', 'EXAMPLE', '1054.73', charge_account=True, phone='555-0100', imported_last_sale=date(2024, 7, 31))
        cls.b = customer('B', 'BLAIR', 'SAMPLE', '383.55', charge_account=True, account_limit=Decimal('500'))
        cls.c = customer('C', 'CASEY', 'DEMO', '-914.69')
        cls.d = customer('D', 'ZERO', 'BALANCE', '0', charge_account=True)
        cls.e = customer('E', 'NO', 'CHARGE', '25.00', charge_account=False)
        cls.brother = Printer.objects.create(name='Test Brother', kind='letter', host='192.0.2.30', port=631)

    def test_owed_credit_all_and_totals(self):
        r = customer_balance.build('owed', 'balance')
        self.assertEqual([row['customer'] for row in r.groups[0].rows], ['ALEX EXAMPLE', 'BLAIR SAMPLE', 'NO CHARGE'])
        self.assertEqual(r.totals['balance'], Decimal('1463.28'))
        r = customer_balance.build('credit')
        self.assertEqual([row['balance'] for row in r.groups[0].rows], [Decimal('-914.69')])
        r = customer_balance.build('all', 'name', charge_only=True)
        self.assertEqual([row['customer'] for row in r.groups[0].rows], ['ALEX EXAMPLE', 'BLAIR SAMPLE'])
        self.assertEqual((r.owed, r.credit), (Decimal('1438.28'), Decimal('0.00')))

    def test_view_exports_print_and_admin_mode(self):
        quick = self.client.get('/reports/customer-balances/')  # opens straight to the list: who owes, largest first
        self.assertContains(quick, 'Customer Balances')
        self.assertLess(quick.content.index(b'ALEX EXAMPLE'), quick.content.index(b'BLAIR SAMPLE'))
        self.assertNotContains(quick, 'CASEY DEMO')
        self.assertContains(quick, '&amp;account_payment=1">Take Payment</a>')  # one click into the payment receipt per row
        r = self.client.get('/reports/customer-balances/', {'show': 'all', 'run': '1'})
        self.assertContains(r, 'ALEX EXAMPLE')
        self.assertContains(r, 'CASEY DEMO')
        for fmt, magic in [('xlsx', b'PK'), ('pdf', b'%PDF')]:
            r = self.client.get('/reports/customer-balances/', {'show': 'all', 'run': '1', 'format': fmt})
            self.assertTrue(r.content.startswith(magic))
        from sales.test_printing import FakeIPP, ipp_reply
        with mock.patch('sales.letter_printing.urllib.request.urlopen', return_value=FakeIPP(ipp_reply())):
            r = self.client.post('/reports/customer-balances/print/', {'show': 'owed', 'printer': self.brother.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertContains(self.client.get('/reports/'), '/reports/customer-balances/')
        sign_in_register(self.client)   # the shared register login is not an admin
        self.assertEqual(self.client.get('/reports/customer-balances/').status_code, 302)


class CustomerDetailTests(TestCase):
    def setUp(self):
        self.c = customer('L', 'DANA', 'EXAMPLE', '2161.63', charge_account=True, use_with_quickbooks=True,
                          street='123 MAIN ST', city='SPRINGFIELD', state='NY', email_opt_in=True)

    def doc(self, number, total, receipt_type=0, kind='sale', status='completed', **extra):
        return Document.objects.create(kind=kind, status=status, customer=self.c, customer_name='DANA EXAMPLE', source_id=f'S{number}',
                                       source_number=number, receipt_type=receipt_type if kind == 'sale' else None,
                                       completed_at=datetime(2026, 9, number % 28 + 1, 12, tzinfo=NY) if kind == 'sale' else None,
                                       subtotal=total, tax=0, total=Decimal(total), **extra)

    def test_statistics_history_and_detail_page(self):
        self.doc(1, '481.45')
        self.doc(2, '100.00', discount=Decimal('5.00'))
        self.doc(3, '-50.00')                       # return
        self.doc(4, '240.00', receipt_type=2)       # deposit: not a sale
        charged = self.doc(5, '2901.63')
        Tender.objects.create(document=charged, position=1, method='account', amount=Decimal('2161.63'), use_in_total=False)
        paid = self.doc(6, '500.00')
        Tender.objects.create(document=paid, position=1, method='account', amount=Decimal('-500.00'), use_in_total=True)
        self.doc(7, '962.89', kind='order', status='open', deposit_taken=Decimal('240.00'))
        s = detail.statistics(self.c)
        self.assertEqual((s['sales'], s['returns'], s['discounts'], s['receipts']), (Decimal('3983.08'), Decimal('50.00'), Decimal('5.00'), 4))
        self.assertEqual(s['average'], Decimal('995.77'))
        self.assertEqual((s['on_order'], s['deposits'], s['balance_due']), (Decimal('962.89'), Decimal('240.00'), Decimal('722.89')))
        rows = detail.history(self.c)
        labels = {r['number']: (r['label'], r['notes']) for r in rows}
        self.assertEqual(labels['4'][0], 'Deposit')
        self.assertEqual(labels['3'][0], 'Return')
        self.assertIn('Charged to Account $2,161.63', labels['5'][1])
        self.assertIn('Payment On Account $500.00', labels['6'][1])
        self.assertEqual(labels['7'][0], 'Sales Order')
        page = self.client.get('/customers/', {'customer': self.c.pk})
        for text in ['Contact Info', 'QuickBooks Options', 'Customer Notes', 'Statistics', 'History (Recent 100)', 'Addresses',
                     'Bill To Address', '$2,161.63', 'Do not mail to Bill To address']:
            with self.subTest(text=text):
                self.assertContains(page, text)
