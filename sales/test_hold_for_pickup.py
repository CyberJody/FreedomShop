# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hold for Pickup on a paid sales receipt, "Awaiting Pickup" status, the Sales History filter,
the printed line, and Mark Picked Up on any later day. The sale itself (money, tax, stock, reports) is unchanged."""
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings

from accounts.cashiers import SESSION_KEY
from accounts.models import Cashier
from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_register
from customers.models import Customer
from reports import daily_totals, ny_periods, sales_tax

from .models import Document, Item, StockMovement
from .printing import Receipt, render_receipt
from .services import WorkflowError, save_document

NY = ZoneInfo('America/New_York')
SALE_DAY = datetime(2026, 10, 22, 11, tzinfo=NY)
OTHER_DAY = datetime(2026, 10, 21, 11, tzinfo=NY)
LATER = date(2026, 10, 27)


class today:
    """Pin a shop's date for a test: store_today in models and in pickup (its own name binding), and the clock that
    stamps completed_at in services, which pickup turns into the sale day. With only models.store_today patched, a sale
    saved "today" checked its est. pickup date against the real calendar, and the test broke once the real date
    passed 10/24/2026."""
    def __init__(self, day):
        when = datetime(day.year, day.month, day.day, 11, tzinfo=NY) if isinstance(day, date) and not isinstance(day, datetime) else day
        self.patches = [mock.patch('sales.models.store_today', return_value=day), mock.patch('sales.pickup.store_today', return_value=day),
                        mock.patch('sales.services.timezone.now', return_value=when)]

    def __enter__(self):
        for p in self.patches: p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches): p.stop()
        return False


def day(d):
    return ny_periods.resolve('custom', '', d, d, d)


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class HoldForPickupTests(TestCase):
    def setUp(self):
        self.stool = Item.objects.create(sku='10001', name='TEST STOOL', description='TEST BAR STOOL', price='100.00',
                                         on_hand='10', tax_rate='7.000', average_cost='40.00')
        self.customer = Customer.objects.create(source_id='r100', full_name='TEST PICKUP', charge_account=True)

    def save(self, lines, payments, action='complete', token=None, version=0, **extra):
        token = token or str(uuid.uuid4())
        data = dict(token=token, version=version, action=action, customer_id=self.customer.pk, lines=lines,
                    payments=payments, **extra)
        return save_document(data, 'sale', token)

    def sell(self, when=SALE_DAY, qty='2', payments=None, **extra):
        total = str((D('100.00') * D(qty) * D('1.07')).quantize(D('0.01')))
        with today(when.date()):
            doc = self.save([dict(item_id=self.stool.pk, quantity=qty, price='100.00')],
                            payments or [dict(method='cash', amount=total)], **extra)
        Document.objects.filter(pk=doc.pk).update(completed_at=when)
        return Document.objects.get(pk=doc.pk)

    def held_sale(self, when=SALE_DAY, est='2026-10-30', **extra):
        return self.sell(when, hold_for_pickup=True, est_pickup_date=est, **extra)

    def cashier_session(self, name='AC'):
        sign_in_register(self.client)
        session = self.client.session
        session[SESSION_KEY] = Cashier.objects.create(name=name, pin_hash=make_password('0123')).pk
        session.save()

    def act(self, doc, action, est=''):
        return self.client.post(f'/pos/sale/{doc.token}/pickup/', dict(action=action, est_pickup_date=est, version=doc.version))

    def printed(self, doc):
        texts = []
        real = Receipt.text

        def spy(self, text, *a, **k):
            texts.append(text)
            return real(self, text, *a, **k)
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(Document.objects.get(pk=doc.pk), reprint=True)
        return texts

    # --- the sale itself is unchanged ---
    def test_held_sale_matches_a_normal_sale_in_money_tax_stock_and_reports(self):
        normal = self.sell(OTHER_DAY)
        stock_after_normal = Item.objects.get(pk=self.stool.pk).on_hand
        held = self.held_sale(SALE_DAY)
        self.assertEqual(Item.objects.get(pk=self.stool.pk).on_hand, stock_after_normal - 2)   # stock deducted the same
        for field in ('subtotal', 'discount', 'tax', 'total', 'tendered', 'payment_method', 'payment_breakdown', 'status'):
            self.assertEqual(getattr(held, field), getattr(normal, field), field)
        moves = lambda d: list(StockMovement.objects.filter(document=d).values_list('item_id', 'quantity'))
        self.assertEqual(moves(held), moves(normal))
        self.assertEqual(moves(held), [(self.stool.pk, D('-2'))])
        self.assertTrue(held.awaiting_pickup)
        self.assertEqual(held.est_pickup_date, date(2026, 10, 30))
        # Daily Totals and Sales Tax: the held sale's day reads exactly like the normal sale's day.
        a, b = daily_totals.build(day(OTHER_DAY.date())), daily_totals.build(day(SALE_DAY.date()))
        self.assertEqual(a.totals, b.totals)
        self.assertEqual([s['amount'] for s in a.summary], [s['amount'] for s in b.summary])
        self.assertEqual(sales_tax.build_daily(day(OTHER_DAY.date())).totals, sales_tax.build_daily(day(SALE_DAY.date())).totals)
        self.assertEqual(b.totals['tax'], D('14.00'))

    def test_mark_picked_up_changes_no_money_tax_or_stock(self):
        held = self.held_sale()
        before = (Document.objects.filter(pk=held.pk).values('subtotal', 'tax', 'total', 'tendered', 'payment_breakdown', 'completed_at').get(),
                  Item.objects.get(pk=self.stool.pk).on_hand, daily_totals.build(day(SALE_DAY.date())).totals)
        self.cashier_session()
        with today(LATER):
            self.assertEqual(self.act(held, 'picked_up').status_code, 302)
        after = (Document.objects.filter(pk=held.pk).values('subtotal', 'tax', 'total', 'tendered', 'payment_breakdown', 'completed_at').get(),
                 Item.objects.get(pk=self.stool.pk).on_hand, daily_totals.build(day(SALE_DAY.date())).totals)
        self.assertEqual(before, after)

    # --- checkout ---
    def test_checkout_box_with_and_without_a_date(self):
        doc = self.held_sale(est='')
        self.assertTrue(doc.hold_for_pickup)
        self.assertIsNone(doc.est_pickup_date)
        self.assertEqual(doc.pickup_status, 'Awaiting Pickup')
        dated = self.held_sale()
        self.assertEqual(dated.pickup_status, 'Awaiting Pickup – est. 10/30')
        plain = self.sell()
        self.assertFalse(plain.hold_for_pickup)
        self.assertEqual(plain.pickup_status, '')

    def test_charge_account_sale_can_be_held(self):
        doc = self.held_sale(payments=[dict(method='account', amount='214.00')])
        self.assertTrue(doc.awaiting_pickup)
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).account_balance, D('214.00'))

    def test_returns_put_on_hold_and_bad_dates_are_refused(self):
        sale = self.sell()
        line = sale.lines.get()
        with today(SALE_DAY.date()), self.assertRaisesMessage(WorkflowError, 'A return or exchange can\'t be held for pickup'):
            self.save([dict(returned_from_line=line.pk, item_id=self.stool.pk, quantity='-1', price='100.00')],
                      [dict(method='cash', amount='-107.00')], hold_for_pickup=True)
        with today(SALE_DAY.date()), self.assertRaisesMessage(WorkflowError, 'Untick it to Put on Hold'):
            self.save([dict(item_id=self.stool.pk, quantity='1', price='100.00')], [], action='hold', hold_for_pickup=True)
        with today(SALE_DAY.date()), self.assertRaisesMessage(WorkflowError, 'can\'t be before the sale'):
            self.save([dict(item_id=self.stool.pk, quantity='1', price='100.00')], [dict(method='cash', amount='107.00')],
                      hold_for_pickup=True, est_pickup_date='2026-10-01')
        with today(SALE_DAY.date()), self.assertRaisesMessage(WorkflowError, 'valid est. pickup date'):
            self.save([dict(item_id=self.stool.pk, quantity='1', price='100.00')], [dict(method='cash', amount='107.00')],
                      hold_for_pickup=True, est_pickup_date='10/30')

    def test_same_day_resave_can_untick_and_cancel_clears_it(self):
        doc = self.held_sale()
        line = doc.lines.get()
        with today(SALE_DAY.date()):
            doc = self.save([dict(id=line.pk, item_id=self.stool.pk, quantity='2', price='100.00')], [dict(method='cash', amount='214.00')],
                            token=str(doc.token), version=doc.version, hold_for_pickup=False)
        self.assertFalse(doc.hold_for_pickup)
        doc = self.held_sale()
        with today(SALE_DAY.date()):
            doc = save_document(dict(token=str(doc.token), version=doc.version, action='cancel'), 'sale', str(doc.token))
        self.assertEqual((doc.status, doc.hold_for_pickup, doc.est_pickup_date), ('cancelled', False, None))
        with today(SALE_DAY.date()):
            response = self.act(doc, 'hold')
        self.assertIn('cancelled', response.url.replace('%27', "'").replace('+', ' '))

    def test_resave_without_the_box_keeps_it(self):
        """Callers that don't send the box (order pickups, older pages) leave the hold for pickup as it is."""
        doc = self.held_sale()
        line = doc.lines.get()
        with today(SALE_DAY.date()):
            doc = self.save([dict(id=line.pk, item_id=self.stool.pk, quantity='2', price='100.00')], [dict(method='cash', amount='214.00')],
                            token=str(doc.token), version=doc.version, notes='Coming Friday with a truck')
        self.assertTrue(doc.awaiting_pickup)
        self.assertEqual(doc.est_pickup_date, date(2026, 10, 30))

    # --- finished receipt, later days, no override ---
    def test_hold_change_date_and_mark_picked_up_on_a_later_day_without_override(self):
        doc = self.sell()
        self.cashier_session('AC')
        with today(LATER):
            self.assertTrue(Document.objects.get(pk=doc.pk).locked)
            page = self.client.get(f'/pos/sale/{doc.token}/')
            self.assertContains(page, 'id="hold-for-pickup-button"')
            response = self.act(doc, 'hold', '2026-10-29')
            self.assertEqual(response.url, f'/pos/sale/{doc.token}/?saved=pickup-held')
            doc.refresh_from_db()
            self.assertEqual((doc.awaiting_pickup, doc.est_pickup_date), (True, date(2026, 10, 29)))
            self.assertEqual(self.act(doc, 'date', '2026-10-30').url, f'/pos/sale/{doc.token}/?saved=pickup-date')
            doc.refresh_from_db()
            self.assertEqual(doc.est_pickup_date, date(2026, 10, 30))
            page = self.client.get(f'/pos/sale/{doc.token}/').content.decode()
            self.assertIn('Awaiting Pickup – est. 10/30', page)
            self.assertIn('id="mark-picked-up"', page)
            self.assertIn('data-confirm="Mark', page)
            when = datetime(2026, 10, 30, 15, tzinfo=NY)
            with mock.patch('django.utils.timezone.now', return_value=when):
                self.assertEqual(self.act(doc, 'picked_up').url, f'/pos/sale/{doc.token}/?saved=pickup-done')
            doc.refresh_from_db()
            self.assertEqual((doc.picked_up_at, doc.picked_up_by, doc.awaiting_pickup), (when, 'AC', False))
            self.assertEqual(doc.pickup_status, 'Completed, picked up 10/30 by AC')
            page = self.client.get(f'/pos/sale/{doc.token}/').content.decode()
            self.assertIn('Completed, picked up 10/30 by AC', page)
            self.assertNotIn('id="mark-picked-up"', page)
            # Only once, and only while awaiting pickup.
            response = self.act(doc, 'picked_up')
            self.assertIn('error=', response.url)
            self.assertIn('already', self.client.get(response.url).context['workflow_error'])
            self.assertIn('error=', self.act(doc, 'date', '2026-11-02').url)

    def test_stale_version_and_held_receipts_are_refused(self):
        doc = self.held_sale()
        self.cashier_session()
        response = self.client.post(f'/pos/sale/{doc.token}/pickup/', dict(action='picked_up', version=doc.version - 1))
        self.assertIn('error=', response.url)
        self.assertTrue(Document.objects.get(pk=doc.pk).awaiting_pickup)
        with today(SALE_DAY.date()):
            held = self.save([dict(item_id=self.stool.pk, quantity='1', price='100.00')], [], action='hold', hold_until='none')
        response = self.act(held, 'hold')
        self.assertIn('error=', response.url)
        self.assertFalse(Document.objects.get(pk=held.pk).hold_for_pickup)

    # --- Sales History and Held Receipts ---
    def test_sales_history_status_and_awaiting_pickup_filter(self):
        no_date = self.held_sale(est='')
        late = self.held_sale(est='2026-11-05')
        soon = self.held_sale(est='2026-10-24')
        plain = self.sell()
        done = self.held_sale()
        Document.objects.filter(pk=done.pk).update(picked_up_at=datetime(2026, 10, 23, 10, tzinfo=NY), picked_up_by='AC')
        with today(SALE_DAY.date()):
            page = self.client.get('/pos/history/', {'status': 'pickup', 'view': 'pages'})
            numbers = [d.number for d in page.context['page'].object_list]
            self.assertEqual(numbers, [soon.number, late.number, no_date.number])  # by est. date, no date last
            html = page.content.decode()
            self.assertIn('<option value="pickup" selected>Awaiting Pickup</option>', html)
            self.assertIn('Awaiting Pickup · est. 10/24', html)
            everything = self.client.get('/pos/history/', {'view': 'pages'}).content.decode()
            self.assertIn('Completed, picked up 10/23 by AC', everything)
            self.assertIn('>Awaiting Pickup<', everything)   # the no-date receipt
            rows = self.client.get('/pos/history/', {'status': 'pickup', 'format': 'rows', 'offset': '0'}).json()
        self.assertEqual([r['token'] for r in rows['rows']], [str(soon.token), str(late.token), str(no_date.token)])
        self.assertEqual(rows['rows'][0]['cells'][2], 'Awaiting Pickup · est. 10/24')
        self.assertNotIn(str(plain.token), str(rows))

    def test_never_in_held_receipts(self):
        self.held_sale()
        with today(SALE_DAY.date()):
            page = self.client.get('/pos/held/', {'period': 'all'})
        self.assertEqual(page.context['page'].paginator.count, 0)

    # --- printed receipt ---
    def test_printed_line_while_awaiting_and_not_after_pickup(self):
        doc = self.held_sale()
        self.assertIn('AWAITING PICKUP - Est. 10/30', self.printed(doc))
        self.assertIn('AWAITING PICKUP', self.printed(self.held_sale(est='')))
        self.assertFalse([t for t in self.printed(self.sell()) if 'AWAITING' in t])
        Document.objects.filter(pk=doc.pk).update(picked_up_at=datetime(2026, 10, 30, 15, tzinfo=NY), picked_up_by='AC')
        self.assertFalse([t for t in self.printed(doc) if 'AWAITING' in t])

    def test_checkout_screen_shows_the_box_on_sales_only(self):
        with today(SALE_DAY.date()):
            sale = self.client.get('/pos/sale/new/').content.decode()
            order = self.client.get('/pos/order/new/').content.decode()
        self.assertIn('id="hold-for-pickup"', sale)
        self.assertIn('Est. pickup date', sale)
        self.assertNotIn('id="hold-for-pickup"', order)
