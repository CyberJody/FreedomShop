# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): on a split-payment receipt the change belongs to the cash tender. The printed receipt, the
receipt screen's saved-payments line and the locked receipt's summary list each tender with the amount handed over and
put "Change" directly under the cash line, whichever order the tenders were entered in. Display only."""
import re
from decimal import Decimal as D
from unittest import mock

from .models import Document
from .printing import Receipt, render_receipt
from .test_returns import RETURN_DAY, ReturnTestCase

VISA = dict(method='credit', amount='100.00', tender_name='Visa')


class ChangeLineTests(ReturnTestCase):
    """Chair $100 + 7 % = $107.00 for one."""

    def ring(self, payments):
        return self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], payments)

    def tender_rows(self, doc):
        rows = []
        with mock.patch.object(Receipt, 'columns', autospec=True,
                               side_effect=lambda self, cells, *a, **k: rows.append(tuple(c[0] for c in cells))):
            render_receipt(Document.objects.get(pk=doc.pk))
        start = next(i for i, row in enumerate(rows) if row[0] == 'RECEIPT TOTAL:') + 1
        return rows[start:]

    def screen(self, doc, element_id):
        html = self.client.get(f'/pos/sale/{doc.token}/').content.decode()
        found = re.search(rf'id="{element_id}">(.*?)</(?:div|p)>', html, re.S)
        self.assertIsNotNone(found, html[html.find('readonly-info') - 20:][:1500] if 'readonly-info' in html else element_id)
        block = found.group(1)
        return re.sub(r'<[^>]+>', '', block).replace('&#x27;', "'")

    def lines(self, doc):
        return [(t['label'], t['amount']) for t in Document.objects.get(pk=doc.pk).tender_lines]

    def test_card_first_then_cash_puts_change_under_cash(self):
        doc = self.ring([VISA, dict(method='cash', amount='20.00')])
        self.assertEqual(doc.change, D('13.00'))
        self.assertEqual(self.lines(doc), [('Visa', D('100.00')), ('Cash', D('20.00')), ('Change', D('13.00'))])
        self.assertEqual(self.tender_rows(doc), [('Visa:', '$100.00'), ('Cash:', '$20.00'), ('Change:', '$13.00')])
        self.assertEqual(self.screen(doc, 'saved-tenders'), 'Visa: $100.00 · Cash: $20.00 · Change: $13.00')

    def test_cash_first_then_card_puts_change_under_cash(self):
        doc = self.ring([dict(method='cash', amount='20.00'), VISA])
        self.assertEqual(self.tender_rows(doc), [('Cash:', '$20.00'), ('Change:', '$13.00'), ('Visa:', '$100.00')])
        self.assertEqual(self.screen(doc, 'saved-tenders'), 'Cash: $20.00 · Change: $13.00 · Visa: $100.00')

    def test_cash_and_check_either_order(self):
        check = dict(method='check', amount='100.00', reference='TEST-9')
        doc = self.ring([check, dict(method='cash', amount='10.00')])
        self.assertEqual(self.tender_rows(doc), [('Check #TEST-9:', '$100.00'), ('Cash:', '$10.00'), ('Change:', '$3.00')])
        doc = self.ring([dict(method='cash', amount='10.00'), check])
        self.assertEqual(self.tender_rows(doc), [('Cash:', '$10.00'), ('Change:', '$3.00'), ('Check #TEST-9:', '$100.00')])

    def test_cash_only_shows_cash_then_change(self):
        doc = self.ring([dict(method='cash', amount='120')])
        self.assertEqual(self.tender_rows(doc), [('Cash:', '$120.00'), ('Change:', '$13.00')])
        exact = self.ring([dict(method='cash', amount='107.00')])
        self.assertEqual(self.tender_rows(exact), [('Cash:', '$107.00'), ('Change:', '$0.00')])

    def test_card_only_has_no_change_line(self):
        doc = self.ring([dict(method='credit', amount='107.00', tender_name='Visa')])
        self.assertEqual(self.tender_rows(doc), [('Visa:', '$107.00')])
        self.assertEqual(self.screen(doc, 'saved-tenders'), 'Visa: $107.00')

    def test_multiple_cards_no_change(self):
        doc = self.ring([dict(method='credit', amount='50.00', tender_name='Visa'),
                         dict(method='credit', amount='57.00', tender_name='MasterCard')])
        self.assertEqual(self.tender_rows(doc), [('Visa:', '$50.00'), ('MasterCard:', '$57.00')])

    def test_locked_receipt_summary_puts_change_under_cash(self):
        doc = self.sell('1', payments=[VISA, dict(method='cash', amount='20.00')])
        card = self.sell('1', payments=[dict(method='credit', amount='107.00', tender_name='Visa')])
        with mock.patch('sales.models.store_today', return_value=RETURN_DAY.date()):   # the next day: locked
            self.assertTrue(Document.objects.get(pk=doc.pk).locked)
            self.assertEqual(self.screen(doc, 'readonly-tenders'),
                             'Visa received: $100.00 · Cash received: $20.00 · Change: $13.00')
            self.assertEqual(self.screen(card, 'readonly-tenders'), 'Visa received: $107.00')

    def test_refund_receipt_unchanged(self):
        sale = self.sell('1', payments=[VISA, dict(method='cash', amount='20.00')])
        refund = self.at_return_day(self.save([self.returned(sale)], [dict(method='cash', amount='-7.00'),
                                                                        dict(method='credit', amount='-100.00')]))
        rows = self.tender_rows(refund)
        self.assertEqual(rows[:2], [('Refund given (Cash):', '$7.00'), ('Refund given (Credit card):', '$100.00')])
        self.assertNotIn('Change:', [r[0] for r in rows])
        self.assertEqual(self.lines(refund), [('Cash', D('-7.00')), ('Credit card', D('-100.00'))])
