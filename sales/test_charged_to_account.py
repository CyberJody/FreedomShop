# SPDX-License-Identifier: AGPL-3.0-or-later
"""a finished receipt's payment line says "Charged to account" for the Account tender and shows the customer's
account balance after the receipt (the printed receipt's "Account Balance"). Screen only; the printout is unchanged."""
import re
from decimal import Decimal as D
from unittest import mock

from .models import Document
from .printing import Receipt, render_receipt
from .test_returns import RETURN_DAY, SALE_DAY, ReturnTestCase


class ChargedToAccountScreenTests(ReturnTestCase):
    def screen(self, doc):
        with mock.patch('sales.models.store_today', return_value=RETURN_DAY.date()):   # next day: the read-only summary
            html = self.client.get(f'/pos/sale/{doc.token}/').content.decode()
        found = re.search(r'id="readonly-tenders">(.*?)</p>', html, re.S)
        self.assertIsNotNone(found)
        return re.sub(r'<[^>]+>', '', found.group(1))

    def printed(self, doc):
        rows = []
        with mock.patch.object(Receipt, 'columns', autospec=True,
                               side_effect=lambda self, cells, *a, **k: rows.append(tuple(c[0] for c in cells))):
            render_receipt(Document.objects.get(pk=doc.pk))
        return rows

    def test_account_charge_reads_charged_with_the_balance(self):
        doc = self.sell('2', payments=[dict(method='account', amount='214.00')])
        self.assertEqual(self.screen(doc), 'Charged to account: $214.00 · Account balance: $214.00')
        # The printout is unchanged: tender line "Account:" plus Previous / new Account Balance.
        rows = self.printed(doc)
        self.assertIn(('Account:', '$214.00'), rows)
        self.assertIn(('Previous Account Balance:', '$0.00'), rows)
        self.assertIn(('Account Balance:', '$214.00'), rows)

    def test_split_cash_and_account_and_balance_as_of_that_receipt(self):
        first = self.sell('1', payments=[dict(method='account', amount='107.00')])
        doc = self.sell('2', payments=[dict(method='cash', amount='20.00'), dict(method='account', amount='194.00')])
        self.assertEqual(self.screen(doc), 'Cash received: $20.00 · Change: $0.00 · Charged to account: $194.00 · Account balance: $301.00')
        self.assertEqual(self.screen(first), 'Charged to account: $107.00 · Account balance: $107.00')   # as of the first receipt

    def test_payment_on_account_keeps_received_and_shows_the_balance(self):
        self.sell('1', payments=[dict(method='account', amount='107.00')])
        payment = self.save([dict(account_payment=True, price='100.00', tax_rate='7', item_name='anything')],
                            [dict(method='cash', amount='100.00')])
        Document.objects.filter(pk=payment.pk).update(completed_at=SALE_DAY)
        self.assertEqual(self.screen(Document.objects.get(pk=payment.pk)), 'Cash received: $100.00 · Change: $0.00 · Account balance: $7.00')

    def test_ordinary_sale_unchanged(self):
        doc = self.sell('1', payments=[dict(method='credit', amount='107.00', tender_name='Visa')])
        self.assertEqual(self.screen(doc), 'Visa received: $107.00')
