# SPDX-License-Identifier: AGPL-3.0-or-later
"""the review blockers on order payments, from regression cases.

 rule: each order payment's tax is fixed on the day the money is received; order edits are never refused for
tax reasons; over an order's life, booked tax = the order's final tax; historical days never move; Sales Tax =
Daily Totals every day.

F1 earlier-day payments are immutable, no back-dating, corrections and deletes are logged.
F2 a refund reverses tax at the ratio booked (never positive, never more than booked) while money stays on the
    order, and settles to the final tax when it empties the order or leaves it paid in full.
F3 a dated $0 "Sales tax adjustment" row squares booked tax with the final tax when an order completes, is
    cancelled, or an edit leaves its balance at exactly $0 — the Account-paid part on the Account side.
"""
import uuid
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.utils import timezone

from customers.models import Customer
from sales.models import Document, Line, OrderPayment, OrderPaymentChange, Tender
from sales.services import (save_order_payment, save_document, WorkflowError, EARLIER_DAY_MESSAGE, DATED_TODAY_MESSAGE,
                            ADJUSTMENT_MESSAGE)
from sales.test_imported_orders import ImportedOrderBase, TODAY, QB_DAY, period
from sales.test_imported_order_review import store_day
from reports import sales_tax, daily_totals
from reports.tax_ledger import order_tax_locks, lock_all_orders, open_charges

YDAY = TODAY - timedelta(days=1)
OLD = TODAY - timedelta(days=40)


class Part4Base(ImportedOrderBase):
    def setUp(self):
        super().setUp()
        lock_all_orders()   # the deploy backfill: the QB deposit's 3.27 is locked on its day

    def day_tax(self, when):
        """Sales Tax and Daily Totals for one day; they must always tie."""
        st = sales_tax.build(period(when.date())).tax
        dt = daily_totals.build(period(when.date())).totals['tax']
        self.assertEqual(st, dt, f'Sales Tax {st} != Daily Totals {dt} on {when.date()}')
        return st

    def booked(self):
        self.order.refresh_from_db()
        return sum((lock.tax for lock in order_tax_locks(self.order).values()), D('0'))

    def adjustments(self):
        return list(self.order.payments.filter(adjustment=True).order_by('pk'))

    def correct(self, payment, when=TODAY, **changes):
        self.order.refresh_from_db()
        data = dict(version=self.order.version, token=str(payment.token), payment_id=str(payment.pk), amount=str(payment.amount),
                    method=payment.method, reference=payment.reference)
        data.update(changes)
        with store_day(when):
            save_order_payment(self.order.pk, data, actor=dict(by='TEST admin', cashier='TEST Pat', address='192.0.2.9'))
        payment.refresh_from_db()
        return payment

    def delete(self, payment, when=TODAY):
        self.order.refresh_from_db()
        with store_day(when):
            save_order_payment(self.order.pk, dict(version=self.order.version, payment_id=str(payment.pk), action='delete'),
                               actor=dict(by='TEST admin'))

    def app_only(self):
        """Drop the QB deposit so the order's money is only what the app recorded."""
        Document.objects.filter(pk=self.deposit.pk).delete()
        Document.objects.filter(pk=self.order.pk).update(deposit_taken=D('0'))
        self.order.refresh_from_db()

    def cancel(self, when=TODAY, method='cash'):
        self.order.refresh_from_db()
        with store_day(when):
            return save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method=method,
                                      deposit_refund_amount=str(self.order.unused_payments)), 'order', self.order.token)


class F1EarlierDayPaymentsAreImmutable(Part4Base):
    def test_reference_only_edit_keeps_yesterdays_lock_and_tax(self):
        p = self.pay('70', when=YDAY)
        self.assertEqual((p.locked_tax, self.day_tax(YDAY)), (D('4.58'), D('4.58')))
        with store_day():
            self.save(tax_exempt=True)                    # the order changes; yesterday must not
        p = self.correct(p, reference='check #123')
        self.assertEqual((p.reference, p.locked_tax, p.amount, p.method), ('check #123', D('4.58'), D('70.00'), 'cash'))
        self.assertEqual(self.day_tax(YDAY), D('4.58'))   # Regression 1: was -3.27 after a reference-only save
        self.assertEqual(self.day_tax(TODAY), D('0.00'))
        change = OrderPaymentChange.objects.get()
        self.assertEqual((change.action, change.order_id, change.payment_id, change.performed_by, change.cashier, change.client_address),
                         ('correct', self.order.pk, p.pk, 'TEST admin', 'TEST Pat', '192.0.2.9'))
        self.assertEqual(change.summary, f'Corrected Cash $70.00 dated {YDAY.date()}: Reference (blank) -> check #123')
        self.assertEqual((change.before['reference'], change.after['reference']), ('', 'check #123'))

    def test_earlier_day_amount_method_date_and_delete_are_refused(self):
        p = self.pay('70', when=YDAY)
        before = self.day_tax(YDAY)
        for changes in (dict(amount='80'), dict(method='check'), dict(payment_date=TODAY.date().isoformat()), dict(payment_date=OLD.date().isoformat())):
            with self.assertRaisesMessage(WorkflowError, EARLIER_DAY_MESSAGE):
                self.correct(p, **changes)
        with self.assertRaisesMessage(WorkflowError, EARLIER_DAY_MESSAGE):
            self.delete(p)
        p.refresh_from_db()
        self.assertEqual((p.amount, p.method, p.locked_tax, p.store_day), (D('70.00'), 'cash', D('4.58'), YDAY.date()))
        self.assertEqual(self.day_tax(YDAY), before)     # Regression: the delete used to erase yesterday's tax
        self.assertEqual(OrderPaymentChange.objects.count(), 0)
        self.assertTrue(OrderPayment.objects.filter(pk=p.pk).exists())

    def test_same_day_payment_is_fully_editable_and_relocks_but_cannot_move_to_another_day(self):
        p = self.pay('70')                                # today
        self.assertEqual(p.locked_tax, D('4.58'))
        p = self.correct(p, amount='100', method='check', check_number='1470')
        self.assertEqual((p.amount, p.method, p.locked_tax), (D('100.00'), 'check', D('6.54')))   # 150 of 214: 9.81 - 3.27
        change = OrderPaymentChange.objects.get()
        self.assertEqual(change.summary, f'Corrected Cash $70.00 dated {TODAY.date()}: Amount $70.00 -> $100.00; Paid by Cash -> Check; Check # (blank) -> 1470')
        p = self.correct(p, payment_date=TODAY.date().isoformat())       # its own day: fine, nothing changes
        self.assertEqual(OrderPaymentChange.objects.count(), 1)
        for other in (YDAY, OLD, TODAY + timedelta(days=1)):
            with self.assertRaisesMessage(WorkflowError, DATED_TODAY_MESSAGE):
                self.correct(p, payment_date=other.date().isoformat())
        p.refresh_from_db()
        self.assertEqual(p.store_day, TODAY.date())
        self.delete(p)
        self.assertFalse(OrderPayment.objects.filter(pk=p.pk).exists())
        change = OrderPaymentChange.objects.order_by('pk').last()
        self.assertEqual((change.action, change.payment, change.summary), ('delete', None, f'Deleted payment: Check #1470 $100.00 dated {TODAY.date()}'))
        self.assertEqual(self.day_tax(TODAY), D('0.00'))

    def test_new_payment_is_dated_today_whatever_date_is_posted(self):
        before = self.day_tax(OLD)
        with self.assertRaisesMessage(WorkflowError, DATED_TODAY_MESSAGE):
            self.pay('100', payment_date=OLD.date().isoformat())     # Review N1: the register's free Date field
        with self.assertRaisesMessage(WorkflowError, DATED_TODAY_MESSAGE):
            self.pay('100', payment_date=YDAY.date().isoformat())
        self.assertEqual((self.order.payments.count(), self.day_tax(OLD)), (0, before))
        p = self.pay('100', payment_date=TODAY.date().isoformat())      # today's date is accepted and ignored
        self.assertEqual((p.store_day, p.locked_tax), (TODAY.date(), D('6.54')))
        self.assertEqual(self.day_tax(TODAY), D('6.54'))

    def test_register_posts_go_through_the_same_rule_and_log_the_workstation(self):
        p = self.pay('70', when=YDAY)
        url = f'/pos/order/{self.order.token}/payment/'
        with store_day():
            r = self.client.post(url, dict(version=self.order.version, token=str(p.token), payment_id=p.pk, amount='80', method='cash', reference=''))
            self.assertIn('This+payment+is+from+an+earlier+day', r['Location'])
            r = self.client.post(url, dict(version=self.order.version, payment_id=p.pk, action='delete'))
            self.assertIn('earlier+day', r['Location'])
            r = self.client.post(url, dict(version=self.order.version, token=str(uuid.uuid4()), amount='5', method='cash', payment_date=YDAY.date().isoformat()))
            self.assertIn('dated+the+day+it+is+recorded', r['Location'])
            r = self.client.post(url, dict(version=self.order.version, token=str(p.token), payment_id=p.pk, amount='70', method='cash', reference='Visa 1234'))
            self.assertNotIn('error', r['Location'])
        p.refresh_from_db()
        self.assertEqual((p.reference, p.amount), ('Visa 1234', D('70.00')))
        change = OrderPaymentChange.objects.get()
        self.assertEqual((change.client_address, change.summary), ('127.0.0.1', f'Corrected Cash $70.00 dated {YDAY.date()}: Reference (blank) -> Visa 1234'))
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertContains(page, 'Payment changes (1)')
        self.assertContains(page, 'Reference (blank) -&gt; Visa 1234')


class F2RefundTaxRule(Part4Base):
    def test_partial_refund_after_exempt_payment_made_taxable_books_no_positive_tax(self):
        self.app_only()
        with store_day(YDAY):
            self.save(tax_exempt=True)
        p = self.pay('50', when=YDAY)                     # exempt: nothing booked
        self.assertEqual(p.locked_tax, D('0.00'))
        with store_day():
            saved = self.save(tax_exempt=False)          # 214.00 / 7.00 again
        self.assertEqual((saved.tax, saved.total), (D('14.00'), D('214.00')))
        r = self.pay('-10')
        self.assertEqual((r.locked_tax, r.locked_taxable, r.locked_nontaxable), (D('0.00'), D('0.00'), D('-10.00')))   # Regression 2: was +2.62
        self.assertEqual((self.day_tax(YDAY), self.day_tax(TODAY), self.booked()), (D('0.00'), D('0.00'), D('0.00')))

    def test_partial_refund_on_an_unchanged_order_reverses_at_the_booked_ratio(self):
        p = self.pay('100', when=YDAY)                    # 150 of 214 with the QB deposit: 9.81 - 3.27 = 6.54
        self.assertEqual(p.locked_tax, D('6.54'))
        r = self.pay('-40')                               # 110 stays: booked 9.81 x 110/150 = 7.19 -> reverse 2.62
        self.assertEqual((r.locked_tax, r.locked_location), (D('-2.62'), 'Springfield'))
        self.assertEqual(self.booked(), D('7.19'))
        self.assertEqual((self.day_tax(YDAY), self.day_tax(TODAY)), (D('6.54'), D('-2.62')))

    def test_refund_of_the_tax_after_exempt_toggle_on_a_paid_order_settles_to_zero(self):
        self.pay('164', when=YDAY)                        # paid in full: 14.00 booked
        with store_day():
            saved = self.save(tax_exempt=True)            # 200.00: overpaid by 14
        self.assertEqual((saved.balance_due, self.booked()), (D('-14.00'), D('14.00')))
        r = self.pay('-14')                               # Review: the case F2's ratio alone would break (-0.92)
        self.assertEqual((r.locked_tax, r.locked_taxable, r.locked_nontaxable, r.locked_location), (D('-14.00'), D('-200.00'), D('200.00'), 'Springfield'))
        self.assertEqual((self.booked(), self.order.balance_due, self.adjustments()), (D('0.00'), D('0.00'), []))
        self.assertEqual((self.day_tax(YDAY), self.day_tax(TODAY)), (D('10.73'), D('-14.00')))

    def test_refund_that_empties_the_order_reverses_exactly_what_was_booked(self):
        self.app_only()
        self.pay('100', when=YDAY)                        # 6.54
        with store_day():
            self.save(tax_exempt=True)
        r = self.pay('-100')
        self.assertEqual((r.locked_tax, r.locked_location, self.booked()), (D('-6.54'), 'Springfield', D('0.00')))

    def test_cancel_after_taxable_toggle_and_a_pickup_ends_at_the_pickups_tax(self):
        # Review N2: exempt order, $200 paid yesterday, made taxable today, one chair picked up, then Cancel.
        self.app_only()
        with store_day(YDAY):
            self.save(tax_exempt=True)
        self.pay('200', when=YDAY)
        with store_day():
            self.save(tax_exempt=False)
        receipt = self.pickup(1)
        self.assertEqual((receipt.total, receipt.tax, self.order.unused_payments), (D('107.00'), D('7.00'), D('93.00')))
        done = self.cancel()
        refund = done.payments.filter(adjustment=False).order_by('pk').last()
        self.assertEqual((done.status, refund.amount, refund.locked_tax), ('cancelled', D('-93.00'), D('0.00')))   # was +7.00 on the refund
        adj = self.adjustments()
        self.assertEqual([(a.locked_tax, a.locked_taxable, a.locked_nontaxable, a.method, a.amount) for a in adj],
                         [(D('7.00'), D('100.00'), D('-107.00'), 'other', D('0.00'))])
        self.assertEqual(self.booked(), D('7.00'))        # the tax on what was picked up
        self.assertEqual((self.day_tax(YDAY), self.day_tax(TODAY)), (D('0.00'), D('7.00')))
        report = sales_tax.build(period(TODAY.date()), detailed=True)
        labels = [(row.type_label, row.tax, row.total) for s in report.sections for row in s.rows]
        self.assertIn(('Sales tax adjustment · Sales Order 5400', D('7.00'), D('0.00')), labels)
        self.assertIn(('Order refund · Sales Order 5400', D('0.00'), D('-93.00')), labels)

    def test_cancel_with_no_pickup_reverses_everything(self):
        self.pay('70', when=YDAY)                         # 120 paid: 7.85 booked
        done = self.cancel()
        refund = done.payments.filter(adjustment=False).order_by('pk').last()
        self.assertEqual((refund.amount, refund.locked_tax, self.booked(), self.adjustments()), (D('-120.00'), D('-7.85'), D('0.00'), []))

    def test_cancel_refund_to_account_keeps_the_split_consistent(self):
        self.pay('70', when=YDAY)
        done = self.cancel(method='account')
        refund = done.payments.filter(adjustment=False).order_by('pk').last()
        self.assertEqual((refund.method, refund.locked_tax, refund.locked_taxable, refund.locked_account_taxable, refund.locked_nontaxable),
                         ('account', D('-7.85'), D('-112.15'), D('-112.15'), D('0.00')))
        self.assertEqual(refund.locked_tax + refund.locked_taxable + refund.locked_nontaxable, refund.amount)


class F3SettlementAdjustment(Part4Base):
    def test_astra_zero_balance_pickup_after_price_and_exempt_edit(self):
        self.pay('164', when=YDAY)                        # 214 paid in full: 14.00 booked (3.27 + 10.73)
        self.assertEqual((self.day_tax(QB_DAY), self.day_tax(YDAY)), (D('3.27'), D('10.73')))
        rows = self.register_rows(); rows[0]['price'] = '107'
        with store_day():
            saved = self.save(rows, tax_exempt=True)      # 2 x 107, exempt: total 214, tax 0, balance 0
        self.assertEqual((saved.total, saved.tax, saved.balance_due), (D('214.00'), D('0.00'), D('0.00')))
        adj = self.adjustments()                          # the zero-balance edit already settles it
        self.assertEqual([(a.locked_tax, a.locked_taxable, a.locked_nontaxable, a.locked_location, a.store_day) for a in adj],
                         [(D('-14.00'), D('-200.00'), D('214.00'), 'Springfield', TODAY.date())])
        self.assertEqual(self.booked(), D('0.00'))
        with store_day():
            self.pickup(2)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.tax, self.order.balance_due, self.booked(), len(self.adjustments())),
                         ('completed', D('0.00'), D('0.00'), D('0.00'), 1))   # Regression 3: booked stayed 14.00
        self.assertEqual((self.day_tax(QB_DAY), self.day_tax(YDAY), self.day_tax(TODAY)), (D('3.27'), D('10.73'), D('-14.00')))
        dt = daily_totals.build(period(TODAY.date()))
        row = next(r for g in dt.groups for r in g.rows if 'Sales tax adjustment' in r['customer'])
        self.assertEqual((row['received'], row['cash'], row['tax']), (D('0.00'), D('0.00'), D('-14.00')))

    def test_adjustment_row_is_shown_on_the_order_and_cannot_be_changed(self):
        self.pay('164', when=YDAY)
        with store_day():
            self.save(tax_exempt=True, lines=[dict(r, price='107') for r in self.register_rows()])
        adj = self.adjustments()[0]
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertContains(page, 'Sales tax adjustment')
        self.assertContains(page, 'Tax −$14.00')
        self.assertNotContains(page, f'name="payment_id" value="{adj.pk}"')   # no form for it
        self.assertEqual(self.order.paid, D('214.00'))                          # not money
        for data in (dict(token=str(adj.token), payment_id=str(adj.pk), amount='1', method='cash'), dict(payment_id=str(adj.pk), action='delete')):
            self.order.refresh_from_db()
            with self.assertRaisesMessage(WorkflowError, ADJUSTMENT_MESSAGE):
                save_order_payment(self.order.pk, dict(version=self.order.version, **data))
        # The order's printout and the pickup receipt list payments only.
        from sales.letter_printing import deposit_rows
        self.assertEqual([r[3] for r in deposit_rows(self.order)], ['QuickBooks', 'Cash'])

    def test_opus_variant_exempt_after_full_payment_waits_for_the_refund(self):
        self.pay('164', when=YDAY)
        with store_day():
            saved = self.save(tax_exempt=True)            # 200.00 total, 214 paid: overpaid, no adjustment yet
        self.assertEqual((saved.balance_due, self.adjustments(), self.booked()), (D('-14.00'), [], D('14.00')))
        with store_day():
            self.pickup(2)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.adjustments()), ('open', []))
        self.pay('-14')                                   # F2 settles it
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.booked(), self.adjustments()), ('completed', D('0.00'), []))

    def test_one_chair_removed_after_full_payment_then_pickup_and_refund(self):
        self.pay('164', when=YDAY)
        rows = self.register_rows(); rows[0]['quantity'] = '1'
        with store_day():
            saved = self.save(rows)                       # 107.00: overpaid by 107
        self.assertEqual((saved.total, saved.balance_due), (D('107.00'), D('-107.00')))
        with store_day():
            self.pickup(1)
        r = self.pay('-107')
        self.order.refresh_from_db()
        self.assertEqual((r.locked_tax, self.booked(), self.order.tax, self.order.status, self.adjustments()), (D('-7.00'), D('7.00'), D('7.00'), 'completed', []))

    def test_account_paid_qb_deposit_adjusts_the_deferred_part_not_collected_tax(self):
        # One of the 19 open orders whose QB deposit was paid by Account: the whole 214 charged to the account.
        Tender.objects.filter(document=self.deposit).update(method='account', amount=D('214'))
        Document.objects.filter(pk=self.deposit.pk).update(total=D('214'), locked_tax=None)
        Document.objects.filter(pk=self.order.pk).update(deposit_taken=D('214'))
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('214'))   # QB: the charge is owed
        self.customer.refresh_from_db()
        lock_all_orders()
        self.deposit.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual((self.deposit.locked_tax, self.deposit.locked_account_taxable, self.order.balance_due), (D('14.00'), D('200.00'), D('0.00')))
        self.assertEqual(self.day_tax(QB_DAY), D('0.00'))                # deferred: nothing collected on the deposit day
        self.assertEqual([(k, charged, owed) for k, charged, owed in open_charges(self.customer)], [(('sale', self.deposit.pk), D('214.00'), D('214.00'))])
        with store_day():
            self.save(tax_exempt=True, lines=[dict(r, price='107') for r in self.register_rows()])   # total 214, tax 0, balance 0
        adj = self.adjustments()
        self.assertEqual([(a.method, a.locked_tax, a.locked_taxable, a.locked_account_taxable, a.locked_nontaxable) for a in adj],
                         [('account', D('-14.00'), D('-200.00'), D('-200.00'), D('214.00'))])
        self.assertEqual(self.booked(), D('0.00'))
        self.assertEqual((self.day_tax(QB_DAY), self.day_tax(TODAY)), (D('0.00'), D('0.00')))   # nothing collected: the charge's tax shrank
        # The customer pays the charge later: it releases no tax now.
        with store_day(TODAY + timedelta(days=3)):
            paid = save_document(dict(action='complete', version=0, customer_id=self.customer.pk, payment_method='cash', tendered='214',
                                      lines=[dict(item_id=None, account_payment=True, item_name='Payment On Account', description='Payment On Account',
                                                  quantity='1', price='214', tax_rate='0')]), 'sale', uuid.uuid4())
        self.assertEqual(self.day_tax(TODAY + timedelta(days=3)), D('0.00'))
        self.assertEqual(open_charges(self.customer), [])

    def test_account_deposit_already_paid_counts_the_adjustment_today(self):
        Tender.objects.filter(document=self.deposit).update(method='account', amount=D('214'))
        Document.objects.filter(pk=self.deposit.pk).update(total=D('214'), locked_tax=None)
        Document.objects.filter(pk=self.order.pk).update(deposit_taken=D('214'))
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('214'))   # QB: the charge is owed
        self.customer.refresh_from_db()
        lock_all_orders()
        with store_day(YDAY):
            save_document(dict(action='complete', version=0, customer_id=self.customer.pk, payment_method='cash', tendered='214',
                               lines=[dict(item_id=None, account_payment=True, item_name='Payment On Account', description='Payment On Account',
                                           quantity='1', price='214', tax_rate='0')]), 'sale', uuid.uuid4())
        self.assertEqual(self.day_tax(YDAY), D('14.00'))                 # paying the charge released its tax
        with store_day():
            self.save(tax_exempt=True, lines=[dict(r, price='107') for r in self.register_rows()])
        self.assertEqual((self.booked(), self.day_tax(YDAY), self.day_tax(TODAY)), (D('0.00'), D('14.00'), D('-14.00')))
        report = sales_tax.build(period(TODAY.date()), detailed=True)
        self.assertIn(('Sales tax adjustment · charge already paid', D('-14.00')), [(row.type_label, row.tax) for s in report.sections for row in s.rows])

    def test_ordinary_edits_and_payments_create_no_adjustment(self):
        # Paid in full, no tax change: nothing to settle. A later chair added leaves a balance: nothing until it is paid.
        self.pay('164', when=YDAY)
        with store_day():
            self.save(notes='Deliver Friday')
        self.assertEqual(self.adjustments(), [])
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        with store_day():
            saved = self.save(rows)
        self.assertEqual((saved.balance_due, self.adjustments()), (D('107.00'), []))
        p = self.pay('107')                               # the closing payment carries the 7.00
        self.assertEqual((p.locked_tax, self.booked(), self.order.tax, self.adjustments()), (D('7.00'), D('21.00'), D('21.00'), []))
        with store_day():
            self.pickup(3)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.adjustments()), ('completed', []))



class Part5ReReview(Part4Base):
    """The follow-up review of part 4 (414dfe3): the Account settlement must split by what is still owed and follow the
    order's section (finding 1), and a refund that leaves the order paid in full or overpaid must settle it (finding 2)."""
    DAY2 = YDAY - timedelta(days=1)
    TMRW = TODAY + timedelta(days=1)

    def poa(self, amount, day):
        with store_day(day):
            return save_document(dict(action='complete', version=0, customer_id=self.customer.pk, payment_method='cash', tendered=str(amount),
                                      lines=[dict(item_id=None, account_payment=True, item_name='Payment On Account', description='Payment On Account',
                                                  quantity='1', price=str(amount), tax_rate='0')]), 'sale', uuid.uuid4())

    def charge(self, exempt, when):
        """The whole $214 order charged to the customer's account on `when`: 2 x 107 exempt or 2 x 100 + 7 %."""
        self.app_only()
        with store_day(when):
            self.save(tax_exempt=exempt, lines=[dict(r, price='107' if exempt else '100') for r in self.register_rows()])
        return self.pay('214', method='account', when=when)

    def flip(self, exempt):
        with store_day(TODAY):
            saved = self.save(tax_exempt=exempt, lines=[dict(r, price='107' if exempt else '100') for r in self.register_rows()])
        self.assertEqual((saved.total, saved.tax, saved.balance_due), (D('214.00'), D('0.00') if exempt else D('14.00'), D('0.00')))
        return saved

    def owed(self):
        self.customer.refresh_from_db()
        return open_charges(self.customer)

    def lots(self):
        """The customer's open charges as the ledger holds them now: (amount, tax, taxable, section)."""
        from reports.tax_ledger import replay, customer_events
        ledger = replay(customer_events(self.customer.pk)).ledgers.get(self.customer.pk)
        return [(lot.amount, lot.tax, lot.taxable, lot.location) for lot in (ledger.lots if ledger else [])]

    def rows(self, when):
        """Sales Tax (detailed) rows of one day by section: {section: [(label, tax, taxable, total)]}, asserting the day
        ties to Daily Totals and that the Exempt section never shows tax."""
        self.day_tax(when)
        report = sales_tax.build(period(when.date()), detailed=True)
        out = {s.location: [(r.type_label, r.tax, r.taxable, r.total) for r in s.rows] for s in report.sections}
        self.assertEqual(sum((r.tax for r in report.sections[1].rows), D('0')), D('0.00'), f'tax under Exempt on {when.date()}: {out}')
        return out

    # ---- finding 1: Account settlement with positive adjustments ------------------------------------------------

    def test_astra_1a_partly_paid_account_charge_made_taxable(self):
        p = self.charge(exempt=True, when=self.DAY2)
        self.assertEqual((p.locked_tax, p.locked_location), (D('0.00'), 'Exempt'))
        self.poa('213', YDAY)
        self.assertEqual(self.rows(YDAY)['Exempt'], [('Payment on account', D('0.00'), D('0.00'), D('213.00'))])
        self.assertEqual(self.lots(), [(D('1.00'), D('0.00'), D('0.00'), 'Exempt')])
        self.flip(exempt=False)
        adj = self.adjustments()
        self.assertEqual([(a.method, a.locked_tax, a.locked_taxable, a.locked_account_taxable, a.locked_nontaxable, a.locked_location) for a in adj],
                         [('account', D('14.00'), D('200.00'), D('200.00'), D('-214.00'), 'Springfield')])
        # The $213 already paid was counted under Exempt yesterday: its share of the new tax is trued up today, under
        # Springfield; the $1 still owed carries its own share (never more tax than its money) and moves to Springfield.
        self.assertEqual(self.day_tax(TODAY), D('13.93'))
        self.assertIn(('Sales tax adjustment · charge already paid', D('13.93'), D('199.07'), D('0.00')), self.rows(TODAY)['Springfield'])
        self.assertEqual(self.lots(), [(D('1.00'), D('0.07'), D('0.93'), 'Springfield')])
        self.assertEqual(self.owed(), [(('order_payment', p.pk), D('214.00'), D('1.00'))])
        self.assertEqual(self.rows(YDAY)['Exempt'], [('Payment on account', D('0.00'), D('0.00'), D('213.00'))])   # history unchanged
        self.poa('1', self.TMRW)
        self.assertEqual(self.day_tax(self.TMRW), D('0.07'))                       # was 14.00 tax on $1 (taxable -13.00)
        self.assertEqual(self.rows(self.TMRW)['Springfield'], [('Payment on account', D('0.07'), D('0.93'), D('1.00'))])
        self.assertEqual((self.booked(), self.lots(), self.owed()), (D('14.00'), [], []))

    def test_astra_1b_unpaid_account_charge_made_taxable_is_collected_under_springfield(self):
        p = self.charge(exempt=True, when=YDAY)
        self.assertEqual(self.lots(), [(D('214.00'), D('0.00'), D('0.00'), 'Exempt')])
        self.flip(exempt=False)
        self.assertEqual(self.day_tax(TODAY), D('0.00'))                           # nothing paid yet: nothing counted
        self.assertEqual(self.lots(), [(D('214.00'), D('14.00'), D('200.00'), 'Springfield')])
        self.poa('214', self.TMRW)
        rows = self.rows(self.TMRW)
        self.assertEqual(rows['Springfield'], [('Payment on account', D('14.00'), D('200.00'), D('214.00'))])   # was under Exempt
        self.assertEqual(rows['Exempt'], [])
        self.assertEqual((self.booked(), self.lots()), (D('14.00'), []))

    def test_exempt_to_taxable_after_the_charge_was_paid_in_full(self):
        self.charge(exempt=True, when=self.DAY2)
        self.poa('214', YDAY)
        self.assertEqual((self.day_tax(YDAY), self.lots()), (D('0.00'), []))
        self.flip(exempt=False)
        self.assertEqual(self.day_tax(TODAY), D('14.00'))
        self.assertIn(('Sales tax adjustment · charge already paid', D('14.00'), D('200.00'), D('0.00')), self.rows(TODAY)['Springfield'])
        self.assertEqual((self.booked(), self.lots(), self.day_tax(YDAY)), (D('14.00'), [], D('0.00')))

    def test_taxable_to_exempt_with_none_of_the_charge_paid(self):
        p = self.charge(exempt=False, when=YDAY)
        self.assertEqual((p.locked_tax, p.locked_account_taxable), (D('14.00'), D('200.00')))
        self.assertEqual(self.lots(), [(D('214.00'), D('14.00'), D('200.00'), 'Springfield')])
        self.flip(exempt=True)
        adj = self.adjustments()
        self.assertEqual([(a.method, a.locked_tax, a.locked_taxable, a.locked_nontaxable, a.locked_location) for a in adj],
                         [('account', D('-14.00'), D('-200.00'), D('214.00'), 'Exempt')])
        self.assertEqual((self.day_tax(TODAY), self.lots()), (D('0.00'), [(D('214.00'), D('0.00'), D('0.00'), 'Exempt')]))
        self.poa('214', self.TMRW)
        rows = self.rows(self.TMRW)
        self.assertEqual((rows['Springfield'], rows['Exempt']), ([], [('Payment on account', D('0.00'), D('0.00'), D('214.00'))]))
        self.assertEqual((self.booked(), self.lots()), (D('0.00'), []))

    def test_taxable_to_exempt_with_part_of_the_charge_paid(self):
        self.charge(exempt=False, when=self.DAY2)
        self.poa('100', YDAY)
        self.assertEqual(self.rows(YDAY)['Springfield'], [('Payment on account', D('6.54'), D('93.46'), D('100.00'))])
        self.assertEqual(self.lots(), [(D('114.00'), D('7.46'), D('106.54'), 'Springfield')])
        self.flip(exempt=True)
        # The $100 paid released 6.54 yesterday: reversed today under Springfield; the $114 still owed carries nothing now.
        self.assertEqual(self.day_tax(TODAY), D('-6.54'))
        self.assertIn(('Sales tax adjustment · charge already paid', D('-6.54'), D('-93.46'), D('0.00')), self.rows(TODAY)['Springfield'])
        self.assertEqual(self.lots(), [(D('114.00'), D('0.00'), D('0.00'), 'Exempt')])
        self.assertEqual(self.rows(YDAY)['Springfield'], [('Payment on account', D('6.54'), D('93.46'), D('100.00'))])   # history unchanged
        self.poa('114', self.TMRW)
        rows = self.rows(self.TMRW)
        self.assertEqual((rows['Springfield'], rows['Exempt']), ([], [('Payment on account', D('0.00'), D('0.00'), D('114.00'))]))
        self.assertEqual((self.booked(), self.lots()), (D('0.00'), []))

    def test_taxable_to_exempt_after_the_charge_was_paid_in_full(self):
        self.charge(exempt=False, when=self.DAY2)
        self.poa('214', YDAY)
        self.assertEqual((self.day_tax(YDAY), self.lots()), (D('14.00'), []))
        self.flip(exempt=True)
        self.assertEqual(self.day_tax(TODAY), D('-14.00'))
        self.assertIn(('Sales tax adjustment · charge already paid', D('-14.00'), D('-200.00'), D('0.00')), self.rows(TODAY)['Springfield'])
        self.assertEqual((self.booked(), self.lots(), self.day_tax(YDAY)), (D('0.00'), [], D('14.00')))

    def test_price_change_on_a_partly_paid_taxable_charge_splits_by_what_is_owed(self):
        # No status change: a $214 charge becomes $321 (3 chairs) — the balance grows, nothing settles until it is paid.
        self.charge(exempt=False, when=self.DAY2)
        self.poa('107', YDAY)
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        with store_day(TODAY):
            saved = self.save(rows)
        self.assertEqual((saved.total, saved.balance_due, self.adjustments()), (D('321.00'), D('107.00'), []))
        self.assertEqual(self.lots(), [(D('107.00'), D('7.00'), D('100.00'), 'Springfield')])
        p = self.pay('107', method='account')                     # the closing charge carries the third chair's 7.00
        self.assertEqual((p.locked_tax, p.locked_account_taxable, self.adjustments()), (D('7.00'), D('100.00'), []))
        self.assertEqual(self.lots(), [(D('107.00'), D('7.00'), D('100.00'), 'Springfield'), (D('107.00'), D('7.00'), D('100.00'), 'Springfield')])

    # ---- finding 2: a refund that leaves the order paid in full or overpaid settles it ----------------------------

    def test_astra_2_refund_leaving_the_order_overpaid_settles_the_final_tax(self):
        self.app_only()
        with store_day(YDAY):
            self.save(tax_exempt=True)
        p = self.pay('300', when=YDAY, confirm_overpayment='1')
        self.assertEqual((p.locked_tax, self.day_tax(YDAY)), (D('0.00'), D('0.00')))
        with store_day(TODAY):
            saved = self.save(tax_exempt=False)                   # 214.00 / 14.00, overpaid by 86: no adjustment yet
        self.assertEqual((saved.balance_due, self.adjustments(), self.booked()), (D('-86.00'), [], D('0.00')))
        r = self.pay('-10')
        self.assertEqual((r.locked_tax, r.locked_taxable, r.locked_nontaxable), (D('0.00'), D('0.00'), D('-10.00')))   # a refund never books positive tax
        adj = self.adjustments()
        self.assertEqual([(a.method, a.locked_tax, a.locked_taxable, a.locked_nontaxable, a.locked_location, a.store_day) for a in adj],
                         [('other', D('14.00'), D('200.00'), D('-214.00'), 'Springfield', TODAY.date())])
        self.order.refresh_from_db()
        self.assertEqual((self.order.balance_due, self.booked(), self.order.tax), (D('-76.00'), D('14.00'), D('14.00')))   # was booked 0.00
        self.assertEqual((self.day_tax(YDAY), self.day_tax(TODAY)), (D('0.00'), D('14.00')))
        rows = self.rows(TODAY)
        self.assertEqual(rows['Springfield'], [('Order refund · Sales Order 5400', D('0.00'), D('0.00'), D('-10.00')),
                                              ('Sales tax adjustment · Sales Order 5400', D('14.00'), D('200.00'), D('0.00'))])
        # The rest of the overpayment goes back later: nothing more to settle.
        r = self.pay('-76')
        self.order.refresh_from_db()
        self.assertEqual((r.locked_tax, self.order.balance_due, self.booked(), len(self.adjustments())), (D('0.00'), D('0.00'), D('14.00'), 1))

    def test_refund_leaving_the_order_overpaid_with_nothing_to_settle_creates_no_adjustment(self):
        self.app_only()
        p = self.pay('300', when=YDAY, confirm_overpayment='1')                            # capped: the order's whole 14.00
        self.assertEqual(p.locked_tax, D('14.00'))
        r = self.pay('-10')
        self.order.refresh_from_db()
        self.assertEqual((r.locked_tax, self.order.balance_due, self.booked(), self.adjustments()), (D('0.00'), D('-76.00'), D('14.00'), []))

    def test_ordinary_saves_and_payments_on_an_overpaid_order_create_no_adjustment(self):
        self.app_only()
        with store_day(YDAY):
            self.save(tax_exempt=True)
        self.pay('300', when=YDAY, confirm_overpayment='1')
        with store_day(TODAY):
            self.save(tax_exempt=False)
        with store_day(TODAY):
            self.save(notes='Deliver Friday')
        p = self.pay('5', confirm_overpayment='1')                                         # more money on an overpaid order: no adjustment; as the next
        self.order.refresh_from_db()                              # payment it carries the order's whole 14.00 (the remainder rule)
        self.assertEqual((self.order.balance_due, self.adjustments(), p.locked_tax, self.booked()), (D('-91.00'), [], D('14.00'), D('14.00')))
        with store_day(TODAY):
            self.pickup(2)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.adjustments()), ('open', []))
        r = self.pay('-91')                                       # the refund that squares it: balance 0, nothing left to settle
        self.order.refresh_from_db()
        self.assertEqual((r.locked_tax, self.order.status, self.booked(), self.order.tax, self.adjustments()),
                         (D('0.00'), 'completed', D('14.00'), D('14.00'), []))

    def test_earlier_account_reference_over_limit(self):
        # Metadata regression check: a reference-only save on an earlier-day Account payment is not an over-limit charge.
        p = self.pay('70', method='account', when=YDAY)
        self.customer.account_limit = D('10')
        self.customer.save()
        self.correct(p, reference='metadata only')
        self.assertEqual(p.reference, 'metadata only')
