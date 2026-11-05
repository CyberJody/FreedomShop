# SPDX-License-Identifier: AGPL-3.0-or-later
from unittest import mock
from .test_returns import ReturnTestCase
from .models import tender_label
from .services import WorkflowError
from .printing import Receipt, render_receipt


class PaymentDetailsTests(ReturnTestCase):
    def test_split_details_survive_save_and_print(self):
        doc = self.sell('1', payments=[dict(method='credit', amount='50.00', tender_name='MasterCard'),
                                      dict(method='check', amount='57.00', reference='TEST-123')])
        doc.refresh_from_db()
        self.assertEqual(doc.payment_breakdown[0]['tender_name'], 'MasterCard')
        self.assertEqual(doc.tender_details[1]['label'], 'Check #TEST-123')
        with mock.patch.object(Receipt, 'columns', autospec=True) as columns:
            render_receipt(doc)
        text = ' '.join(str(c) for c in columns.call_args_list)
        self.assertIn('MasterCard', text)
        self.assertIn('Check #TEST-123', text)
        response = self.client.get(f'/pos/sale/{doc.token}/')
        self.assertContains(response, 'Check #TEST-123')

    def test_invalid_card_and_reference_rejected_atomically(self):
        with self.assertRaises(WorkflowError):
            self.sell('1', payments=[dict(method='credit', amount='107', tender_name='Unknown')])
        with self.assertRaises(WorkflowError):
            self.sell('1', payments=[dict(method='check', amount='107', reference='x'*101)])
        self.assertEqual(self.on_hand(self.chair), 5)

    def test_refund_retains_card_name(self):
        sale = self.sell('1')
        doc = self.save([self.returned(sale)], [dict(method='credit', amount='-107', tender_name='Visa')])
        self.assertEqual(tender_label(doc.payment_breakdown[0]), 'Visa')
