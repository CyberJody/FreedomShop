# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal as D
from .test_returns import ReturnTestCase
from .models import GiftCertificate, GiftRedemption
from .services import WorkflowError, returnable


class GiftTests(ReturnTestCase):
    def issue(self, number='TEST-GIFT', amount='200'):
        return self.save([dict(item_name='GIFT CERTIFICATE', description='TEST gift certificate', certificate_number=number, quantity='1', price=amount, tax_rate='7')], [dict(method='cash', amount=amount)])

    def test_issue_redeem_partially_and_cancel_redemption(self):
        issued = self.issue()
        self.assertEqual(issued.tax, 0)
        self.assertFalse(returnable(issued.lines.get()))
        sale = self.sell('1', payments=[dict(method='gift', amount='107', reference='TEST-GIFT')])
        self.assertEqual(GiftCertificate.objects.get().balance, D('93'))
        self.assertEqual(GiftRedemption.objects.get().amount, D('107'))
        self.save([], [], action='cancel', token=sale.token, version=sale.version)
        self.assertEqual(GiftCertificate.objects.get().balance, D('200'))
        self.save([], [], action='cancel', token=issued.token, version=issued.version)
        self.assertFalse(GiftCertificate.objects.exists())

    def test_unknown_duplicate_spent_and_overspend(self):
        with self.assertRaises(WorkflowError):
            self.save([dict(item_id=self.chair.pk, certificate_number='TEST-INVALID', quantity='1', price='100')], [dict(method='cash', amount='100')])
        self.assertFalse(GiftCertificate.objects.exists())
        with self.assertRaises(WorkflowError):
            self.sell('1', payments=[dict(method='gift', amount='107', reference='unknown')])
        issue = self.issue(amount='107')
        with self.assertRaises(WorkflowError): self.issue(amount='107')
        with self.assertRaises(WorkflowError):
            self.sell('2', payments=[dict(method='gift', amount='214', reference='TEST-GIFT')])
        self.sell('1', payments=[dict(method='gift', amount='107', reference='TEST-GIFT')])
        with self.assertRaises(WorkflowError):
            self.sell('1', payments=[dict(method='gift', amount='107', reference='TEST-GIFT')])
        with self.assertRaises(WorkflowError):
            self.save([], [], action='cancel', token=issue.token, version=issue.version)
        self.assertEqual(GiftCertificate.objects.get().balance, 0)

    def test_same_receipt_split_cannot_overspend(self):
        self.issue(amount='100')
        with self.assertRaises(WorkflowError):
            self.sell('1', payments=[dict(method='gift', amount='50', reference='TEST-GIFT'), dict(method='gift', amount='57', reference='TEST-GIFT')])
        self.assertEqual(GiftCertificate.objects.get().balance, 100)

    def test_held_certificate_has_no_balance(self):
        self.save([dict(item_name='GIFT CERTIFICATE', description='TEST gift', certificate_number='HELD', quantity='1', price='100')], [], action='hold')
        self.assertFalse(GiftCertificate.objects.exists())

    def test_lookup_and_list(self):
        self.issue()
        self.assertEqual(self.client.get('/pos/gift-certificates/lookup/?number=TEST-GIFT').json()['balance'], '200.00')
        self.assertContains(self.client.get('/pos/gift-certificates/'), 'TEST-GIFT')
