# SPDX-License-Identifier: AGPL-3.0-or-later
"""finished receipts show (and accept) only the actions that apply.

Cancelled: no Cancel / Put on Hold / Save, not even through an Admin Override; Print stays. Completed: no Put on Hold
(the same-day correction save and Cancel Receipt stay). The server refuses the hidden actions too.
"""
import json

from django.test import override_settings

from accounts.signin_helpers import sign_in_admin

from .models import Document, ReceiptOverride
from .services import WorkflowError, save_document
from .test_receipt_lock import LockTestCase, SALE_DAY, NEXT_DAY, SESSIONS, today, D


class FinishedReceiptServiceTests(LockTestCase):
    def test_completed_receipt_cannot_be_put_on_hold(self):
        sale = self.sell()
        line = sale.lines.get()
        with today(SALE_DAY.date()), self.assertRaisesMessage(WorkflowError, 'A completed receipt cannot be put on hold.'):
            self.save([dict(id=line.pk, item_id=self.chair.pk, quantity='2', price='100.00')], [], action='hold',
                      token=str(sale.token), version=sale.version)
        sale.refresh_from_db()
        self.assertEqual((sale.status, self.on_hand(self.chair)), ('completed', D('3')))

    def test_cancelled_receipt_refuses_save_hold_and_cancel(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            cancelled = save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)
            line = cancelled.lines.get()
            for action in ('complete', 'hold'):
                with self.subTest(action=action), self.assertRaisesMessage(WorkflowError, 'This receipt was cancelled.'):
                    self.save([dict(id=line.pk, item_id=self.chair.pk, quantity='2', price='100.00')],
                              [dict(method='cash', amount='214.00')], action=action, token=str(sale.token), version=cancelled.version)
            with self.assertRaisesMessage(WorkflowError, 'This receipt was cancelled.'):
                save_document(dict(action='cancel', version=cancelled.version), 'sale', sale.token)
        doc = Document.objects.get(pk=sale.pk)
        self.assertEqual((doc.status, doc.version, self.on_hand(self.chair)), ('cancelled', cancelled.version, D('5')))

    def test_admin_override_cannot_bring_back_a_cancelled_past_receipt(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            cancelled = save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)
        with today(NEXT_DAY), self.assertRaisesMessage(WorkflowError, 'This receipt was cancelled.'):
            self.save([dict(id=cancelled.lines.get().pk, item_id=self.chair.pk, quantity='2', price='100.00')],
                      [dict(method='cash', amount='214.00')], token=str(sale.token), version=cancelled.version,
                      override=dict(reason='bring it back', by='pat'))
        self.assertEqual(Document.objects.get(pk=sale.pk).status, 'cancelled')
        self.assertFalse(ReceiptOverride.objects.exists())

    def test_same_day_correction_and_cancel_still_work(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            edited = self.edit(sale, '1')
            self.assertEqual(edited.status, 'completed')
            self.assertEqual(save_document(dict(action='cancel', version=edited.version), 'sale', sale.token).status, 'cancelled')


@SESSIONS
class FinishedReceiptScreenTests(LockTestCase):
    def setUp(self):
        super().setUp()
        sign_in_admin(self.client)

    def buttons(self, page):
        html = page.content.decode()
        actions = html[html.index('<div class="actions">'):]
        actions = actions[:actions.index('</div>')]
        return actions

    def test_completed_same_day_has_no_put_on_hold(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            actions = self.buttons(self.client.get(f'/pos/sale/{sale.token}/'))
        self.assertNotIn('data-action="hold"', actions)
        self.assertIn('data-action="cancel"', actions)
        self.assertIn('data-action="complete"', actions)

    def test_cancelled_shows_print_only(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)
            page = self.client.get(f'/pos/sale/{sale.token}/')
        actions = self.buttons(page)
        for action in ('hold', 'cancel', 'complete'):
            self.assertNotIn(f'data-action="{action}"', actions)
        self.assertIn('Make a Sale', actions)
        self.assertTrue(page.context['readonly'])
        self.assertNotIn('type="button" data-return-open>', page.content.decode())
        with today(NEXT_DAY):
            page = self.client.get(f'/pos/sale/{sale.token}/?override=1')
        self.assertFalse(page.context['overriding'])
        self.assertNotIn('id="admin-override"', page.content.decode())
        self.assertNotIn('data-action="complete"', self.buttons(page))

    def test_server_refuses_hold_posted_for_a_completed_receipt(self):
        sale = self.sell()
        line = sale.lines.get()
        payload = dict(token=str(sale.token), version=sale.version, action='hold', customer_id=self.customer.pk,
                       lines=[dict(id=line.pk, item_id=self.chair.pk, quantity='2', price='100.00')], payments=[])
        with today(SALE_DAY.date()):
            response = self.client.post(f'/pos/sale/{sale.token}/', data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'A completed receipt cannot be put on hold.')
