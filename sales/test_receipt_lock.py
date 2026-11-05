# SPDX-License-Identifier: AGPL-3.0-or-later
"""Completed receipts lock after their store day.

QB never edits a saved receipt; corrections are returns dated today. In the app a completed receipt can be edited or
cancelled on its own store day (America/New_York); from the next day it is read-only and Accept Return is the way to
correct it. A signed-in admin can override with a typed reason, which is kept as a ReceiptOverride.
"""
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.sessions.middleware import SessionMiddleware
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register

from customers.models import Customer

from .models import Document, Item, Line, ReceiptOverride
from .services import WorkflowError, save_document

NY = ZoneInfo('America/New_York')
SALE_DAY = datetime(2026, 10, 9, 21, 30, tzinfo=NY)  # 9:30 PM in the store = 10/10 01:30 UTC: still the 10/9 store day
NEXT_DAY = SALE_DAY.date() + timedelta(days=1)
SESSIONS = override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)  # sessions + request.user (admins sign in)


def today(day):
    return mock.patch('sales.models.store_today', return_value=day)


class LockTestCase(TestCase):
    def setUp(self):
        self.chair = Item.objects.create(sku='7001', name='TEST CHAIR', description='TEST CHAIR', price='100.00', on_hand='5',
                                         tax_rate='7.000', average_cost='40.00')
        self.lamp = Item.objects.create(sku='7002', name='TEST LAMP', description='TEST LAMP', price='50.00', on_hand='5',
                                        tax_rate='7.000', average_cost='20.00')
        self.customer = Customer.objects.create(source_id='lock', full_name='TEST LOCK')

    def save(self, lines, payments, action='complete', token=None, version=0, **extra):
        token = token or str(uuid.uuid4())
        override = extra.pop('override', None)
        data = dict(token=token, version=version, action=action, customer_id=self.customer.pk, lines=lines, payments=payments, **extra)
        return save_document(data, 'sale', token, override=override)

    def sell(self):
        with today(SALE_DAY.date()):
            doc = self.save([dict(item_id=self.chair.pk, quantity='2', price='100.00')], [dict(method='cash', amount='214.00')])
        Document.objects.filter(pk=doc.pk).update(completed_at=SALE_DAY)
        return Document.objects.get(pk=doc.pk)

    def edit(self, doc, qty='1', **extra):
        line = doc.lines.get()
        total = (D('100.00') * D(qty) * D('1.07')).quantize(D('0.01'))
        return self.save([dict(id=line.pk, item_id=self.chair.pk, quantity=qty, price='100.00')], [dict(method='cash', amount=str(total))],
                         token=str(doc.token), version=doc.version, **extra)

    def on_hand(self, item):
        return Item.objects.get(pk=item.pk).on_hand


class ReceiptLockServiceTests(LockTestCase):
    def test_same_store_day_edit_and_cancel_still_work(self):
        sale = self.sell()
        self.assertEqual(sale.store_day, SALE_DAY.date())  # 9:30 PM local, not the UTC date
        with today(SALE_DAY.date()):
            self.assertFalse(sale.locked)
            edited = self.edit(sale, '1')
            self.assertEqual((edited.total, self.on_hand(self.chair)), (D('107.00'), D('4')))
            save_document(dict(action='cancel', version=edited.version), 'sale', edited.token)
        self.assertEqual((Document.objects.get(pk=sale.pk).status, self.on_hand(self.chair)), ('cancelled', D('5')))
        self.assertFalse(ReceiptOverride.objects.exists())

    def test_next_day_edit_and_cancel_are_refused(self):
        sale = self.sell()
        with today(NEXT_DAY):
            self.assertTrue(Document.objects.get(pk=sale.pk).locked)
            message = f'{sale.numbered_label} was completed on 10/9/2026. A receipt can only be changed on the day it was completed. To correct it, use Accept Return.'
            with self.assertRaisesMessage(WorkflowError, message):
                self.edit(sale, '1')
            with self.assertRaisesMessage(WorkflowError, 'use Accept Return'):
                save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)
            with self.assertRaisesMessage(WorkflowError, 'use Accept Return'):
                self.save([dict(id=sale.lines.get().pk, item_id=self.chair.pk, quantity='2', price='100.00')], [], action='hold',
                          token=str(sale.token), version=sale.version)
        sale.refresh_from_db()
        self.assertEqual((sale.status, sale.total, sale.version, self.on_hand(self.chair)), ('completed', D('214.00'), 1, D('3')))

    def test_cancelled_receipt_stays_cancelled_after_its_day(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)
        sale.refresh_from_db()
        with today(NEXT_DAY), self.assertRaisesMessage(WorkflowError, 'use Accept Return'):
            self.edit(sale, '2')

    def test_return_from_a_locked_receipt(self):
        sale = self.sell()
        with today(NEXT_DAY):
            ret = self.save([dict(returned_from_line=sale.lines.get().pk, quantity='-1')], [dict(method='cash', amount='-107.00')])
            self.assertEqual((ret.total, self.on_hand(self.chair)), (D('-107.00'), D('4')))
            self.assertFalse(ret.locked)  # the return itself is today's receipt
        Document.objects.filter(pk=ret.pk).update(completed_at=SALE_DAY + timedelta(days=1))
        with today(NEXT_DAY + timedelta(days=1)):
            self.assertTrue(Document.objects.get(pk=ret.pk).locked)

    def test_held_order_and_imported_documents(self):
        with today(SALE_DAY.date()):
            held = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], [], action='hold')
        with today(NEXT_DAY):
            self.assertFalse(held.locked)  # never completed: still editable
            self.save([dict(id=held.lines.get().pk, item_id=self.chair.pk, quantity='1', price='100.00')], [dict(method='cash', amount='107.00')],
                      token=str(held.token), version=held.version)
        qb = Document.objects.create(kind='sale', status='completed', source_id='qb-lock', source_number=9, receipt_type=0,
                                     customer=self.customer, completed_at=SALE_DAY, total=D('1'))
        self.assertFalse(qb.locked)  # read-only anyway, with its own message
        with today(NEXT_DAY), self.assertRaisesMessage(WorkflowError, 'imported from QuickBooks are read-only'):
            save_document(dict(action='cancel', version=qb.version, override=True), 'sale', qb.token, override=dict(reason='x'))

    def test_override_with_reason_is_logged(self):
        sale = self.sell()
        with today(NEXT_DAY):
            edited = self.edit(sale, '1', override=dict(reason='  Rang 2 chairs, customer took 1 (manager OK)  ', by='pat', address='192.0.2.5'))
        self.assertEqual((edited.total, self.on_hand(self.chair)), (D('107.00'), D('4')))
        record = ReceiptOverride.objects.get()
        self.assertEqual((record.document_id, record.action, record.reason, record.performed_by, record.client_address),
                         (sale.pk, 'save', 'Rang 2 chairs, customer took 1 (manager OK)', 'pat', '192.0.2.5'))
        self.assertEqual((record.before['total'], record.after['total'], record.before['lines'][0]['quantity']), ('214.00', '107.00', '2'))
        self.assertIn('Total $214.00 -> $107.00', record.summary)
        self.assertIn('TEST CHAIR: 2 x $100.00 -> 1 x $100.00', record.summary)
        self.assertIn("Payments cash $214.00 -> cash $107.00", record.summary)
        self.assertEqual(Document.objects.get(pk=sale.pk).completed_at, SALE_DAY)  # still dated its own day
        with today(NEXT_DAY):
            cancelled = save_document(dict(action='cancel', version=edited.version), 'sale', sale.token, override=dict(reason='Duplicate', by='pat'))
        self.assertEqual((cancelled.status, self.on_hand(self.chair)), ('cancelled', D('5')))
        self.assertEqual(ReceiptOverride.objects.filter(action='cancel').get().summary.split(';')[0], 'Status completed -> cancelled')

    def test_override_without_reason_is_refused(self):
        sale = self.sell()
        with today(NEXT_DAY):
            for reason in (None, '', '   ', 5):
                with self.assertRaisesMessage(WorkflowError, 'Enter the reason for this Admin Override'):
                    self.edit(sale, '1', override=dict(reason=reason, by='pat'))
            with self.assertRaisesMessage(WorkflowError, 'cannot be put on hold'):
                self.save([dict(id=sale.lines.get().pk, item_id=self.chair.pk, quantity='2', price='100.00')], [], action='hold',
                          token=str(sale.token), version=sale.version, override=dict(reason='why'))
        self.assertFalse(ReceiptOverride.objects.exists())
        self.assertEqual(Document.objects.get(pk=sale.pk).total, D('214.00'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReceiptOverride.objects.create(document=sale, action='save', reason='')


@SESSIONS
class ReceiptLockViewTests(LockTestCase):
    def post(self, doc, payload):
        return self.client.post(f'/pos/sale/{doc.token}/', data=json.dumps(payload), content_type='application/json')

    def payload(self, doc, qty='1', **extra):
        line = doc.lines.get()
        total = str((D('100.00') * D(qty) * D('1.07')).quantize(D('0.01')))
        return dict(token=str(doc.token), version=doc.version, action='complete', customer_id=self.customer.pk,
                    lines=[dict(id=line.pk, item_id=self.chair.pk, quantity=qty, price='100.00')],
                    payments=[dict(method='cash', amount=total)], **extra)

    def admin_on(self):
        sign_in_admin(self.client)

    def test_register_login_and_pin_cashier_cannot_override(self):
        from django.contrib.auth.hashers import make_password
        from accounts.cashiers import SESSION_KEY
        from accounts.models import Cashier
        sale = self.sell()
        sign_in_register(self.client)
        session = self.client.session
        session[SESSION_KEY] = Cashier.objects.create(name='TEST Cashier', pin_hash=make_password('0123')).pk
        session['admin_mode'] = True   # the retired File-menu flag means nothing now
        session.save()
        with today(NEXT_DAY):
            page = self.client.get(f'/pos/sale/{sale.token}/')
            self.assertContains(page, 'id="locked-notice"')
            self.assertNotContains(page, 'id="admin-override"')
            self.assertFalse(self.client.get(f'/pos/sale/{sale.token}/?override=1').context['overriding'])
            response = self.post(sale, self.payload(sale, override=True, override_reason='because'))
            self.assertEqual((response.status_code, response.json()['error']),
                             (400, 'Sign in as an admin to override a past receipt.'))
        self.assertFalse(ReceiptOverride.objects.exists())
        self.assertEqual(Document.objects.get(pk=sale.pk).total, D('214.00'))

    def test_same_day_page_is_editable(self):
        sale = self.sell()
        with today(SALE_DAY.date()):
            page = self.client.get(f'/pos/sale/{sale.token}/')
            self.assertFalse(page.context['readonly'])
            self.assertNotContains(page, 'id="locked-notice"')
            self.assertEqual(self.post(sale, self.payload(sale, '1')).status_code, 200)

    def test_locked_page_and_post_refused(self):
        sale = self.sell()
        with today(NEXT_DAY):
            page = self.client.get(f'/pos/sale/{sale.token}/')
            self.assertTrue(page.context['readonly'])
            self.assertTrue(page.context['initial']['readonly'])
            self.assertContains(page, 'id="locked-notice"')
            self.assertContains(page, 'was completed on 10/9/2026. A receipt can only be changed on the day it was completed.')
            self.assertContains(page, f'href="/pos/sale/new/?return_from={sale.token}">Accept Return</a>')
            self.assertNotContains(page, 'id="admin-override"')  # nobody signed in: no Admin Override
            self.assertNotContains(page, 'data-action="cancel"')
            self.assertFalse(self.client.get(f'/pos/sale/{sale.token}/?override=1').context['overriding'])
            response = self.post(sale, self.payload(sale))
            self.assertEqual(response.status_code, 400)
            self.assertIn('use Accept Return', response.json()['error'])
            cancel = self.post(sale, dict(token=str(sale.token), version=sale.version, action='cancel'))
            self.assertIn('use Accept Return', cancel.json()['error'])
            # The override flag alone does nothing without an admin sign-in.
            response = self.post(sale, self.payload(sale, override=True, override_reason='because'))
            self.assertEqual(response.json()['error'], 'Sign in as an admin to override a past receipt.')
        self.assertEqual(Document.objects.get(pk=sale.pk).total, D('214.00'))

    def test_admin_override_through_the_page(self):
        sale = self.sell()
        self.admin_on()
        with today(NEXT_DAY):
            page = self.client.get(f'/pos/sale/{sale.token}/')
            self.assertContains(page, '<a class="button" id="admin-override" href="?override=1">Admin Override</a>')
            page = self.client.get(f'/pos/sale/{sale.token}/?override=1')
            self.assertTrue(page.context['overriding'])
            self.assertFalse(page.context['readonly'])
            self.assertTrue(page.context['initial']['override'])
            self.assertContains(page, 'Override reason *')
            self.assertContains(page, '>Save Override</button>')
            self.assertNotContains(page, '>Put on Hold</button>')
            missing = self.post(sale, self.payload(sale, override=True, override_reason=' '))
            self.assertEqual((missing.status_code, missing.json()['error']), (400, 'Enter the reason for this Admin Override before saving.'))
            ok = self.post(sale, self.payload(sale, override=True, override_reason='Wrong quantity rung'))
            self.assertEqual(ok.status_code, 200, ok.content)
            record = ReceiptOverride.objects.get()
            self.assertEqual((record.reason, record.performed_by, record.client_address), ('Wrong quantity rung', 'TEST Admin', '127.0.0.1'))
            history = self.client.get(f'/pos/sale/{sale.token}/')
            self.assertContains(history, 'Override history')
            self.assertContains(history, 'Wrong quantity rung')
            self.assertContains(history, 'Total $214.00 -&gt; $107.00')
