# SPDX-License-Identifier: AGPL-3.0-or-later
"""review : one regression test per finding on open sales orders imported from QuickBooks POS.
The order fixture is `ImportedOrderBase` (2 x $100 chair + 7 % = 214.00, QB deposit $50 on 10/14)."""
import json
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.utils import timezone

from sales.models import Document, Line, DiscountReason
from sales.services import save_document, save_order_payment, fulfill_order, WorkflowError
from sales.test_imported_orders import ImportedOrderBase, TODAY, QB_DAY, period
from sales.test_qb_half_up import register_math, run_node


@contextmanager
def store_day(when=TODAY):
    """The store day the guards treat as 'today' (deposit_tax_guard reads timezone.localdate)."""
    with mock.patch('django.utils.timezone.now', return_value=when):
        yield


class Finding1GenuinePercent(ImportedOrderBase):
    def test_5400_genuine_5_percent_reapplies_after_a_quantity_edit(self):
        # 5400: 6,884.75 at 5 % = 344.24 (QB's figure is the percent of the lines, so it is a real percent).
        DiscountReason.objects.get_or_create(name='Sale')
        Line.objects.filter(pk=self.line.pk).update(quantity=1, price=D('2829.95'), original_price=D('2829.95'), receipt_discount=D('141.50'))
        Line.objects.create(document=self.order, position=1, sku='SOFA', item_name='SOFA', description='Sofa', quantity=1,
                            price=D('4054.80'), original_price=D('4054.80'), tax_rate=D('7'), receipt_discount=D('202.74'), source_id='QBSO-1:1')
        Document.objects.filter(pk=self.order.pk).update(subtotal=D('6884.75'), discount_percent=D('5'), discount=D('344.24'), tax=D('457.84'), total=D('6998.35'))
        rows = self.register_rows(); rows[0]['quantity'] = '2'
        with store_day():
            saved = self.save(rows)
        self.assertEqual((saved.subtotal, saved.discount_percent, saved.discount), (D('9714.70'), D('5.00'), D('485.74')))
        self.assertEqual(sum(l.receipt_discount for l in saved.lines.all()), D('485.74'))


class Finding2ShippingCharge(ImportedOrderBase):
    def setUp(self):
        super().setUp()
        # QB put $12.00 shipping (taxed) in the total that no line carries: 200 + 12 + 14.84 tax = 226.84.
        Document.objects.filter(pk=self.order.pk).update(tax=D('14.84'), total=D('226.84'))
        self.order.refresh_from_db()

    def test_edit_and_pickup_refused_payment_cancel_and_note_save_allowed(self):
        self.assertEqual(self.order.qb_other_charge, D('12.00'))
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        with store_day(), self.assertRaisesMessage(WorkflowError, '$12.00 shipping/other charge from QuickBooks'):
            self.save(rows)
        self.pay('176.84')
        with self.assertRaisesMessage(WorkflowError, 'shipping/other charge'):
            self.pickup(2)
        with store_day():
            saved = self.save(notes='Deliver Friday')
        self.assertEqual((saved.notes, saved.subtotal, saved.tax, saved.total), ('Deliver Friday', D('200.00'), D('14.84'), D('226.84')))
        done = save_document(dict(action='cancel', version=saved.version, refund_deposit=True, deposit_refund_method='cash', deposit_refund_amount='226.84'), 'order', saved.token)
        self.assertEqual((done.status, done.paid, done.unused_payments), ('cancelled', D('0.00'), D('0.00')))


class Finding3EarlierDepositTax(ImportedOrderBase):
    """Resolved by locking: the QB deposit's tax is fixed on its day;
    edits are never refused; the tax difference is booked on the next payment, on that payment's day."""

    def setUp(self):
        super().setUp()
        from reports.tax_ledger import lock_all_orders
        self.assertEqual(lock_all_orders(), dict(orders=1, payments_locked=1, payments_already_locked=0))   # the deploy backfill
        self.deposit.refresh_from_db()
        self.assertEqual((self.deposit.locked_tax, self.deposit.locked_taxable, self.deposit.locked_nontaxable, self.deposit.locked_location),
                         (D('3.27'), D('46.73'), D('0.00'), 'Springfield'))

    def day_tax(self, when=QB_DAY):
        from reports import sales_tax, daily_totals
        st, dt = sales_tax.build(period(when.date())).tax, daily_totals.build(period(when.date())).totals['tax']
        self.assertEqual(st, dt)
        return st

    def booked(self):
        from reports.tax_ledger import order_tax_locks
        return [lock.tax for lock in order_tax_locks(self.order).values()]

    def test_5402_add_a_line_and_toggle_exempt_both_save_the_deposit_day_keeps_its_tax(self):
        self.assertEqual(self.day_tax(), D('3.27'))
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        with store_day():
            saved = self.save(rows)                       # 3 x 100 + 7 % = 321.00: allowed
        self.assertEqual((saved.tax, saved.total, saved.balance_due), (D('21.00'), D('321.00'), D('271.00')))
        self.assertEqual(self.day_tax(), D('3.27'))
        with store_day():
            saved = self.save(tax_exempt=True)            # allowed too
        self.assertEqual((saved.tax_exempt, saved.tax, saved.total, saved.balance_due), (True, D('0.00'), D('300.00'), D('250.00')))
        self.assertEqual(self.day_tax(), D('3.27'))
        self.assertEqual(self.day_tax(TODAY), D('0.00'))

    def test_extra_tax_lands_on_the_payment_that_pays_the_new_balance(self):
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        with store_day():
            self.save(rows)
        payment = self.pay('271')                         # pays the balance, today
        self.assertEqual((payment.locked_tax, payment.locked_taxable, payment.locked_nontaxable), (D('17.73'), D('253.27'), D('0.00')))
        self.assertEqual((self.day_tax(), self.day_tax(TODAY)), (D('3.27'), D('17.73')))
        self.assertEqual(sum(self.booked()), self.order.tax)    # 21.00 over the order's life, to the cent

    def test_exempt_toggle_after_the_deposit_reverses_its_tax_on_the_final_payment(self):
        with store_day():
            self.save(tax_exempt=True)
        payment = self.pay('150')                         # 200.00 total, 50 deposit: pays it off
        self.assertEqual((payment.locked_tax, payment.locked_location), (D('-3.27'), 'Springfield'))   # reversed where it was reported
        self.assertEqual((self.day_tax(), self.day_tax(TODAY)), (D('3.27'), D('-3.27')))
        self.assertEqual((sum(self.booked()), self.order.tax, self.order.balance_due), (D('0.00'), D('0.00'), D('0.00')))
        from reports import sales_tax
        report = sales_tax.build(period(TODAY.date()))
        self.assertEqual([(s.location, s.tax, s.total) for s in report.sections], [('Springfield', D('-3.27'), D('150.00')), ('Exempt', D('0.00'), D('0.00'))])
        refund = self.pay('-20')                          # money back on the exempt order now: no tax, Exempt section
        self.assertEqual((refund.locked_tax, refund.locked_location), (D('0.00'), 'Exempt'))

    def test_final_payment_settles_the_tax_exactly_after_rounding_on_earlier_ones(self):
        first = self.pay('70', when=TODAY - timedelta(days=1))   # 120 of 214: 7.85 cumulative, 3.27 already booked
        self.assertEqual(first.locked_tax, D('4.58'))
        last = self.pay('94')
        self.assertEqual(last.locked_tax, D('6.15'))              # 14.00 - 7.85
        self.assertEqual((sum(self.booked()), self.order.balance_due, self.order.status), (D('14.00'), D('0.00'), 'open'))

    def test_cancel_with_refund_reverses_exactly_what_was_booked(self):
        self.pay('70', when=TODAY - timedelta(days=1))
        with store_day():
            done = save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method='cash', deposit_refund_amount='120'), 'order', self.order.token)
        refund = done.payments.order_by('pk').last()
        self.assertEqual((done.status, refund.amount, refund.locked_tax), ('cancelled', D('-120.00'), D('-7.85')))
        self.assertEqual(sum(self.booked()), D('0.00'))
        self.assertEqual(self.day_tax(TODAY), D("-7.85"))   # the refund is dated the day it is recorded (the pinned clock)

    def test_a_deposit_taken_today_keeps_its_figures_too(self):
        # Locked figures never move, not even on the deposit's own day: the edit's difference goes on the next payment.
        with store_day(QB_DAY + timedelta(hours=2)):
            saved = self.save(tax_exempt=True)
        self.assertEqual((saved.tax_exempt, saved.tax), (True, D('0.00')))
        self.assertEqual(self.day_tax(), D('3.27'))


class Finding4DepositMismatch(ImportedOrderBase):
    def setUp(self):
        super().setUp()
        # QB says $50 was taken, but the linked QB deposit receipts add up to $40 (1393 / 2890 in the 10/18 data).
        Document.objects.filter(pk=self.deposit.pk).update(total=D('40'))
        self.order.refresh_from_db()

    def test_cancel_pickup_and_refund_refused_payment_allowed(self):
        self.assertEqual(self.order.qb_deposit_mismatch, (D('50.00'), D('40.00')))
        msg = 'QuickBooks shows a $50.00 deposit on this order, but its QuickBooks deposit receipts add up to $40.00'
        with self.assertRaisesMessage(WorkflowError, msg):
            save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method='cash', deposit_refund_amount='50'), 'order', self.order.token)
        payment = self.pay('57')
        self.assertEqual((payment.amount, self.order.paid), (D('57.00'), D('107.00')))
        with self.assertRaisesMessage(WorkflowError, msg):
            self.pickup(1)
        with self.assertRaisesMessage(WorkflowError, msg):
            self.pay('-10')
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.paid, self.order.unfilled), ('open', D('107.00'), 2))


class Finding5DepositAlreadyUsed(ImportedOrderBase):
    def test_972_shaped_order_cannot_use_the_qb_deposit_twice(self):
        # QB took $200 and already used all of it on a pickup there (QB pickup receipts are not app fulfillments).
        Document.objects.filter(pk=self.order.pk).update(deposit_taken=D('200'), deposit_used=D('200'))
        Document.objects.filter(pk=self.deposit.pk).update(total=D('200'))
        self.order.refresh_from_db()
        self.assertEqual((self.order.paid, self.order.applied_payments, self.order.unused_payments), (D('200.00'), D('200.00'), D('0.00')))
        self.pay('14')   # the balance due
        self.assertEqual((self.order.balance_due, self.order.unused_payments), (D('0.00'), D('14.00')))
        with self.assertRaisesMessage(WorkflowError, 'Record another $200.00 on this order before this pickup (QuickBooks already used $200.00 of its deposit on a pickup there).'):
            self.pickup(2)
        with self.assertRaisesMessage(WorkflowError, "QuickBooks already used $200.00 of this order's deposit on a pickup"):
            self.pay('-20')
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.paid, self.order.applied_payments, self.order.unused_payments), ('open', D('214.00'), D('200.00'), D('14.00')))
        self.assertGreaterEqual(self.order.unused_payments, 0)


class Finding6RegisterTotals(ImportedOrderBase):
    def js_totals(self, rows=None, discount_percent=None, tax_exempt=None):
        from sales.views import document_data
        self.order.refresh_from_db()
        data = document_data(self.order)
        lines = data['lines'] if rows is None else rows
        pct = data['discount_percent'] if discount_percent is None else discount_percent
        exempt = data['tax_exempt'] if tax_exempt is None else tax_exempt
        script = (register_math() + '\nconst lines=' + json.dumps(lines) + ';const rec=' + json.dumps(data['qb_recorded'])
                  + f';const t=qbOrderTotals(lines,{json.dumps(pct)},{json.dumps(exempt)},"0",rec);'
                  'console.log(JSON.stringify([t.sub,t.discount,t.tax,t.total,t.recorded===true]));')
        return json.loads(run_node(script))

    def test_4444_opening_total_keeps_the_qb_shipping_until_a_money_edit(self):
        # 4444: 2 x 20.00 = 40.00, $12 shipping, tax 3.64 on 52.00 -> QB total 55.64 (the plain register math gave 42.80).
        Line.objects.filter(pk=self.line.pk).update(price=D('20'), original_price=D('20'))
        Document.objects.filter(pk=self.order.pk).update(subtotal=D('40'), tax=D('3.64'), total=D('55.64'))
        self.assertEqual(self.js_totals(), [4000, 0, 364, 5564, True])
        with store_day():
            saved = self.save(notes='note only')
        self.assertEqual(saved.total, D('55.64'))
        self.assertEqual(self.js_totals(), [4000, 0, 364, 5564, True])
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        self.assertEqual(self.js_totals(rows), [6000, 0, 420, 6420, False])   # the server refuses this edit (finding 2)

    def test_3401_typed_dollar_discount_matches_the_server_open_note_and_edit(self):
        # 3401: 24,503.65 with QB's typed $1,196.00 off (stored as 4.88 %; 4.88 % would be 1,195.78), total 24,939.19.
        Line.objects.filter(pk=self.line.pk).update(quantity=1, price=D('24503.65'), original_price=D('24503.65'), receipt_discount=D('1196'))
        Document.objects.filter(pk=self.order.pk).update(subtotal=D('24503.65'), discount_percent=D('4.88'), discount=D('1196'), tax=D('1631.54'), total=D('24939.19'))
        self.assertEqual(self.js_totals(), [2450365, 119600, 163154, 2493919, True])
        with store_day():
            saved = self.save(notes='note only')
        self.assertEqual((saved.discount, saved.total), (D('1196.00'), D('24939.19')))
        self.assertEqual(self.js_totals(), [2450365, 119600, 163154, 2493919, True])
        rows = self.register_rows(); rows[0]['quantity'] = '2'
        js = self.js_totals(rows)
        with store_day():
            saved = self.save(rows)
        self.assertEqual(js[:4], [int(v * 100) for v in (saved.subtotal, saved.discount, saved.tax, saved.total)])
        self.assertEqual((saved.discount, js[4]), (D('1196.00'), False))


class Finding7ClosedOrderHistory(ImportedOrderBase):
    def test_closed_qb_order_keeps_its_app_account_charge_on_printout_history_and_page(self):
        from customers.detail import history
        from sales.letter_printing import Pages, render_sales_order
        self.pay('164', method='account', reference='House account')
        Document.objects.filter(pk=self.order.pk).update(status='completed')
        self.order.refresh_from_db()
        self.assertTrue(self.order.imported)
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(self.order)
        texts = [step[3] for steps in pages for step in steps if step[0] == 'text']
        self.assertEqual(texts[texts.index('Charged to Account:') + 1], '$164.00')
        row = next(r for r in history(self.customer) if r['label'] == 'Sales Order')
        self.assertIn('Charged to Account $164.00', row['notes'])
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertTrue(page.context['readonly'])
        self.assertContains(page, 'Deposits and Payments')
        self.assertContains(page, 'Deposit taken in QuickBooks POS $50.00')
        self.assertContains(page, 'House account')
        self.assertContains(page, '$164.00')
        self.assertNotContains(page, 'Payments and Pickup')
