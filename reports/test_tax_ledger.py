# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared cash-basis tax ledger (//): Sales Tax ties to Daily Totals to the cent, order payments add up
to the order's tax, Account parts wait until paid, and 's "On account · $owed" status comes from the same state."""
import os
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from django.db.models import F
from django.test import TestCase, override_settings
from django.utils import timezone

from customers.models import Customer
from sales.models import Document, Line, OrderPayment, Tender, money
from sales.services import save_document
from sales.test_returns import RETURN_DAY, SALE_DAY, ReturnTestCase, day as one_day

from . import daily_totals, ny_periods, sales_tax, sales_tax_exports, tax_ledger
from .tax_ledger import EXEMPT, DEFAULT_LOCATION, ZERO

NY = ZoneInfo('America/New_York')
WHEN = datetime(2026, 9, 24, 12, tzinfo=NY)


def period(start, end=None):
    return ny_periods.resolve('custom', '', start, start, end or start)


def portion(take, part, whole):
    """The cent rule restated for the reference model: a proportional share, half-up; taking the whole takes it all."""
    return part if take == whole else money(take * part / whole)


class DayReference:
    """rules written out independently of `tax_ledger`, event by event, to say on which DAY and under which
    location every cent of tax and money must land (review 3: lifetime totals alone cannot catch wrong timing).
    Per customer: `open` charges oldest first (amount, tax, location, charge key, qb = QuickBooks' balance accounts
    for it), unapplied `credits` oldest first, and `paid` allocations in order. Tax counts on money received, the day
    it is received: a receipt's tax × money in ÷ total; a payment pays the oldest charges first and releases their
    tax, the rest is a prepayment taxed at 7/107 (zero for an Exempt customer) and held as credit; a credit funds the
    next charge on that charge's day (its own tax reversed, the charge's tax counted, no money); a refund hands back
    credit newest first, then undoes the newest payments (reopening their charges), then reopens pre-data debt; a
    return credited to the account first comes off its own charge if still owed (nothing counted — no tax was ever
    collected), the rest is a credit carrying the collected tax (capped at what the credit can hold, the excess
    reversed that day); QuickBooks' balance below the imported receipts settles imported charges off the books at
    the backup moment and holds the rest as credit, neither with any tax."""

    def __init__(self):
        self.tax, self.money, self.received = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        self.before = {DEFAULT_LOCATION: [ZERO, ZERO], EXEMPT: [ZERO, ZERO]}     # [money, tax] QuickBooks accounted for before the data
        self.state = defaultdict(lambda: dict(open=[], credits=[], paid=[]))

    def count(self, day, loc, amount, tax):
        self.tax[(day, loc)] += tax
        self.money[(day, loc)] += amount

    def settle(self, day, cust):
        s = self.state[cust]
        while s['credits'] and s['open']:
            c, o = s['credits'][0], s['open'][0]
            take = min(c['amt'], o['amt'])
            ct, ot = portion(take, c['tax'], c['amt']), portion(take, o['tax'], o['amt'])
            if c['kind'] == 'opening':
                self.before[o['loc']][0] += take
                self.before[o['loc']][1] += ot
            else:
                self.count(day, c['loc'], -take, -ct)
                self.count(day, o['loc'], take, ot)
            s['paid'].append(dict(amt=take, tax=ot, loc=o['loc'], key=o['key'], qb=o['qb']))
            c['amt'], c['tax'], o['amt'], o['tax'] = c['amt'] - take, c['tax'] - ct, o['amt'] - take, o['tax'] - ot
            s['credits'] = [x for x in s['credits'] if x['amt']]
            s['open'] = [x for x in s['open'] if x['amt']]

    def sale(self, day, cust, total, tax, cash, account, loc, key, imported):
        m = portion(cash, tax, total) if total else ZERO
        self.count(day, loc, cash, m)
        self.received[day] += cash
        if account:
            self.state[cust]['open'].append(dict(amt=account, tax=tax - m, loc=loc, key=key, qb=imported))
            self.settle(day, cust)

    def estimate(self, amount, exempt):
        return (ZERO if exempt else money(amount * 7 / 107)), (EXEMPT if exempt else DEFAULT_LOCATION)

    def poa(self, day, cust, amount, exempt):
        s = self.state[cust]
        self.received[day] += amount
        rem = amount
        while rem and s['open']:
            o = s['open'][0]
            take = min(rem, o['amt'])
            t = portion(take, o['tax'], o['amt'])
            self.count(day, o['loc'], take, t)
            s['paid'].append(dict(amt=take, tax=t, loc=o['loc'], key=o['key'], qb=o['qb']))
            o['amt'], o['tax'], rem = o['amt'] - take, o['tax'] - t, rem - take
            s['open'] = [x for x in s['open'] if x['amt']]
        if rem:
            est, loc = self.estimate(rem, exempt)
            self.count(day, loc, rem, est)
            s['credits'].append(dict(amt=rem, tax=est, loc=loc, kind='prepaid'))

    def refund(self, day, cust, amount, exempt):
        s = self.state[cust]
        self.received[day] -= amount
        rem = amount
        while rem and s['credits']:
            c = s['credits'][-1]
            take = min(rem, c['amt'])
            ct = portion(take, c['tax'], c['amt'])
            self.count(day, c['loc'], -take, -ct)
            if c['kind'] == 'opening':
                self.before[c['loc']][0] -= take
                self.before[c['loc']][1] -= ct
            c['amt'], c['tax'], rem = c['amt'] - take, c['tax'] - ct, rem - take
            s['credits'] = [x for x in s['credits'] if x['amt']]
        while rem and s['paid']:
            a = s['paid'][-1]
            take = min(rem, a['amt'])
            at = portion(take, a['tax'], a['amt'])
            self.count(day, a['loc'], -take, -at)
            s['open'].insert(0, dict(amt=take, tax=at, loc=a['loc'], key=a['key'], qb=a['qb']))
            a['amt'], a['tax'], rem = a['amt'] - take, a['tax'] - at, rem - take
            s['paid'] = [x for x in s['paid'] if x['amt']]
        if rem:
            est, loc = self.estimate(rem, exempt)
            self.count(day, loc, -rem, -est)
            s['open'].insert(0, dict(amt=rem, tax=est, loc=loc, key=None, qb=True))

    def return_credit(self, day, cust, value, tax, loc, key):
        s = self.state[cust]
        credit_amt, credit_tax, group_amt, group_tax = value, tax, value, tax
        if key is not None:
            for o in [x for x in s['open'] if x['key'] == key]:
                take = min(group_amt, o['amt'], credit_amt)
                if take <= 0:
                    break
                gone = min(max(portion(take, group_tax, group_amt), o['tax'] - (o['amt'] - take)), o['tax'])
                group_amt, group_tax = group_amt - take, group_tax - portion(take, group_tax, group_amt)
                o['amt'], o['tax'] = o['amt'] - take, o['tax'] - gone
                credit_amt, credit_tax = credit_amt - take, credit_tax - gone
            s['open'] = [x for x in s['open'] if x['amt']]
        carried = min(max(credit_tax, ZERO), credit_amt)
        if carried != credit_tax:
            self.count(day, loc, ZERO, carried - credit_tax)
        if credit_amt:
            s['credits'].append(dict(amt=credit_amt, tax=carried, loc=loc, kind='return'))
        self.settle(day, cust)

    def reconcile(self, day, cust, amount, exempt):
        s = self.state[cust]
        rem = amount
        for o in [x for x in s['open'] if x['qb']]:
            if rem <= 0:
                break
            take = min(rem, o['amt'])
            t = portion(take, o['tax'], o['amt'])
            self.before[o['loc']][0] += take
            self.before[o['loc']][1] += t
            o['amt'], o['tax'], rem = o['amt'] - take, o['tax'] - t, rem - take
        s['open'] = [x for x in s['open'] if x['amt']]
        if rem > 0:
            est, loc = self.estimate(rem, exempt)
            s['credits'].append(dict(amt=rem, tax=est, loc=loc, kind='opening'))
            self.settle(day, cust)


class LedgerFixture(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='led', first_name='Ledger', charge_account=True)
        self.counter = 0

    def doc(self, total='107', tax='7', payments=(('cash', '107'),), when=WHEN, customer=None, imported=False,
            poa=False, order=False, lines=None, **kwargs):
        self.counter += 1
        customer = self.customer if customer is None else customer
        obj = Document.objects.create(kind='order' if order else 'sale', status='open' if order else 'completed',
                                      customer=customer or None, customer_name=customer.display_name if customer else 'WALK-IN',
                                      completed_at=None if order else when,
                                      subtotal=money(D(total) - D(tax)), total=money(total), tax=money(tax),
                                      payment_breakdown=[] if imported or order else [{'method': k, 'amount': v} for k, v in payments],
                                      source_id=f'import-{self.counter}' if imported else None, **kwargs)
        Document.objects.filter(pk=obj.pk).update(created_at=when)
        obj.created_at = when
        if imported:
            for pos, (method, amount) in enumerate(payments):
                Tender.objects.create(document=obj, position=pos, method=method, amount=money(amount))
        if poa:
            Line.objects.create(document=obj, position=1, item_name='Payment On Account', description='Payment',
                                quantity=1, price=money(total), tax_rate=0)
        elif lines is None and not order and D(total) != 0:
            Line.objects.create(document=obj, position=1, item_name='ITEM', description='Item', quantity=1,
                                price=money(D(total) - D(tax)), tax_rate=7 if D(tax) else 0)
        for pos, (name, amount, rate) in enumerate(lines or [], start=1):
            Line.objects.create(document=obj, position=pos, item_name=name, description=name, quantity=-1 if D(amount) < 0 else 1,
                                price=abs(money(amount)), tax_rate=rate, source_id=f'line-{obj.pk}-{pos}' if imported else None)
        return obj

    def both(self, start, end=None):
        p = period(start, end)
        st = sales_tax.build(p, detailed=True)
        dt = daily_totals.build(p)
        self.assertEqual(st.tax, dt.totals['tax'], f'Sales Tax {st.tax} vs Daily Totals {dt.totals["tax"]} for {p.label}')
        return st, dt


class TaxLedgerTests(LedgerFixture):
    def test_part_charged_receipt_taxed_as_paid_across_a_quarter_boundary(self):
        # 8/31: $1,080.65 incl. $70.70 tax; $500 card + $580.65 on account. 9/1: paid off.
        sale_day = datetime(2026, 8, 31, 15, tzinfo=NY)
        self.doc(total='1080.65', tax='70.70', payments=(('credit', '500'), ('account', '580.65')), when=sale_day)
        self.doc(total='580.65', tax='0', payments=(('check', '580.65'),), poa=True, when=datetime(2026, 9, 1, 10, tzinfo=NY))
        q2, _ = self.both(date(2026, 6, 1), date(2026, 8, 31))
        q3, dt3 = self.both(date(2026, 9, 1), date(2026, 11, 30))
        self.assertEqual(q2.tax, D('32.71'))            # 500 × 70.70 ÷ 1080.65
        self.assertEqual(q3.tax, D('37.99'))            # the rest, to the cent
        self.assertEqual(q2.tax + q3.tax, D('70.70'))
        self.assertEqual((q2.charged_to_account, q2.charged_to_account_tax), (D('580.65'), D('37.99')))
        self.assertEqual((q3.payments_on_account, q3.payments_on_account_tax, q3.estimated_count), (D('580.65'), D('37.99'), 0))
        row = next(r for r in q2.sections[0].rows if r.kind == 'sale')
        self.assertEqual((row.type_label, row.total, row.tax), ('On account', D('500.00'), D('32.71')))
        self.assertEqual(row.gross - row.discount, row.taxable + row.nontaxable)
        self.assertEqual(row.gross - row.discount + row.tax, row.total)
        poa = next(r for r in q3.sections[0].rows if r.kind == 'account')
        self.assertEqual((poa.type_label, poa.total, poa.tax, poa.taxable), ('Payment on account', D('580.65'), D('37.99'), D('542.66')))

    def test_fully_charged_receipt_counts_nothing_until_paid(self):
        self.doc(payments=(('account', '107'),))
        st, dt = self.both(date(2026, 9, 24))
        self.assertEqual((st.tax, st.receipts), (D('0'), 1))
        self.assertEqual(dt.totals['received'], D('0'))
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=WHEN + timedelta(days=5))
        st2, _ = self.both(date(2026, 9, 29))
        self.assertEqual(st2.tax, D('7.00'))

    def test_three_payment_order_sums_exactly_to_order_tax(self):
        order = self.doc(order=True, total='1070', tax='70', when=WHEN - timedelta(days=10))
        for i, amount in enumerate(('356.67', '356.67', '356.66')):
            OrderPayment.objects.create(order=order, amount=D(amount), method='credit', created_at=WHEN + timedelta(days=i))
        taxes = []
        for i in range(3):
            st, _ = self.both(date(2026, 9, 24 + i))
            taxes.append(st.tax)
        # Cumulative shares: each payment within a cent of its own 7/107 share, the three adding up to exactly 70.00
        # (per-payment rounding would give 23.33 × 3 = 69.99, the drift in qbpos-study-2026-10-12/sales-orders-deposits.md).
        self.assertEqual(taxes, [D('23.33'), D('23.34'), D('23.33')])
        self.assertEqual(sum(taxes), D('70.00'))

    def test_order_payment_beyond_the_total_carries_no_tax_and_refund_of_excess_none_either(self):
        order = self.doc(order=True, total='1070', tax='70', when=WHEN - timedelta(days=1))
        OrderPayment.objects.create(order=order, amount=D('1200'), method='check', created_at=WHEN)
        OrderPayment.objects.create(order=order, amount=D('-130'), method='check', created_at=WHEN + timedelta(days=1))
        st, _ = self.both(date(2026, 9, 24))
        self.assertEqual(st.tax, D('70.00'))
        st2, _ = self.both(date(2026, 9, 25))
        self.assertEqual(st2.tax, D('0.00'))

    def test_account_paid_deposit_waits_and_cancel_with_deposit_refund_nets_to_zero(self):
        order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='Ledger',
                                        subtotal=100, tax=7, total=107)
        Line.objects.create(document=order, position=0, quantity=1, price=100, tax_rate=7)
        Document.objects.filter(pk=order.pk).update(created_at=WHEN - timedelta(days=1))
        OrderPayment.objects.create(order=order, amount=50, method='cash', created_at=WHEN)
        OrderPayment.objects.create(order=order, amount=57, method='account', created_at=WHEN + timedelta(minutes=1))
        st, dt = self.both(date(2026, 9, 24))
        self.assertEqual((st.tax, st.charged_to_account, st.charged_to_account_tax), (D('3.27'), D('57.00'), D('3.73')))
        self.assertEqual(dt.totals['received'], D('50'))
        done = save_document(dict(action='cancel', version=order.version, refund_deposit=True, deposit_refund_method='cash',
                                  deposit_refund_amount='107'), 'order', order.token)
        self.assertEqual(done.status, 'cancelled')
        refund = done.payments.latest('pk')
        OrderPayment.objects.filter(pk=refund.pk).update(created_at=WHEN + timedelta(days=1))
        st2, dt2 = self.both(date(2026, 9, 25))
        self.assertEqual(dt2.totals['received'], D('-107'))
        # Refund in cash returns the whole order's tax share (the Account charge stays on the ledger until paid).
        self.assertEqual(st2.tax, D('-7.00'))
        combined, _ = self.both(date(2026, 9, 24), date(2026, 9, 25))
        self.assertEqual(combined.tax, D('-3.73'))

    def test_overpayment_is_estimated_at_exactly_7_over_107_and_flagged(self):
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))
        self.doc(total='321', tax='0', payments=(('cash', '321'),), poa=True)
        st, dt = self.both(date(2026, 9, 24))
        self.assertEqual(st.tax, D('7.00') + D('14.00'))          # 7 released + 214 × 7/107
        self.assertEqual((st.estimated_count, st.estimated_total, st.estimated_tax), (1, D('214.00'), D('14.00')))
        self.assertEqual(dt.estimated, 1)
        self.assertIn('· estimated', [r.type_label for r in st.sections[0].rows if r.kind == 'account'][0])

    def test_return_credited_to_account_reduces_the_next_payments_tax(self):
        self.doc(total='508.20', tax='33.24', payments=(('account', '508.20'),), when=WHEN - timedelta(days=2))
        returned = self.doc(total='0', tax='-16.62', payments=(), imported=True, when=WHEN - timedelta(days=1),
                            lines=[('NOI-MISC', '-237.48', 7), ('Payment On Account', '254.10', 0)])
        Tender.objects.create(document=returned, position=1, method='account', amount=D('-254.10'), use_in_total=True)
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-254.10'))   # QB saw only the credit
        self.doc(total='254.10', tax='0', payments=(('cash', '254.10'),), poa=True)
        st_return, _ = self.both(date(2026, 9, 23))
        self.assertEqual(st_return.tax, D('0'))
        row = st_return.sections[0].rows[0]
        self.assertEqual(row.type_label, 'Credited to account $254.10')
        st_pay, _ = self.both(date(2026, 9, 24))
        self.assertEqual(st_pay.tax, D('16.62'))

    def test_exempt_customer_payment_without_history_is_zero(self):
        exempt = Customer.objects.create(source_id='ex', first_name='Museum', tax_location='Exempt', tax_exempt=True)
        self.doc(total='100', tax='0', payments=(('account', '100'),), customer=exempt, tax_location='Exempt', when=WHEN - timedelta(days=1))
        self.doc(total='300', tax='0', payments=(('check', '300'),), poa=True, customer=exempt, tax_location='Exempt')
        st, dt = self.both(date(2026, 9, 24))
        self.assertEqual(st.tax, D('0'))
        self.assertEqual(st.sections[1].parts['account'].total, D('300'))
        self.assertEqual((st.estimated_count, st.estimated_tax), (1, D('0')))

    def test_gift_redemption_is_not_received_but_its_tax_counts(self):
        self.doc(payments=(('gift', '107'),))
        self.doc(payments=(('gift', '50'), ('cash', '57')), when=WHEN + timedelta(hours=1))
        st, dt = self.both(date(2026, 9, 24))
        self.assertEqual((dt.totals['received'], dt.totals['gift'], dt.totals['tax']), (D('57'), D('157'), D('14')))
        self.assertEqual((st.tax, st.gift_total, st.gift_tax), (D('14'), D('157'), D('10.27')))
        self.assertTrue(any('Gift certificates redeemed: $157.00' in n for n in __import__('reports.sales_tax_exports', fromlist=['x'])._notes(st)))

    def test_single_undecodable_tender_is_solved_from_the_total(self):
        doc = self.doc(imported=True, total='16770.27', tax='1097.12', payments=(('credit', '6770.27'), ('credit', '10000')))
        Tender.objects.filter(document=doc, amount=D('10000')).update(amount=None)
        st, dt = self.both(date(2026, 9, 24))
        self.assertEqual((st.tax, st.derived_tenders), (D('1097.12'), 1))
        self.assertEqual(dt.totals['card'], D('16770.27'))

    def test_random_ledgers_tie_to_the_cent_and_add_up_across_periods(self):
        rng = random.Random(1310)
        customers = [self.customer] + [Customer.objects.create(source_id=f'r{i}', first_name=f'R{i}', charge_account=True,
                                                               tax_location='Exempt' if i == 3 else '', tax_exempt=i == 3) for i in range(4)]
        orders = []
        start = datetime(2026, 8, 20, 10, tzinfo=NY)
        for i in range(120):
            when = start + timedelta(hours=rng.randint(0, 24 * 40))
            customer = rng.choice(customers)
            exempt = customer.tax_exempt
            roll = rng.random()
            if roll < 0.15:
                self.doc(total=str(rng.randint(10, 500)), tax='0', payments=(('check', None),), poa=True, when=when, customer=customer)
                doc = Document.objects.latest('pk')
                doc.payment_breakdown = [{'method': 'check', 'amount': str(doc.total)}]
                doc.save()
            elif roll < 0.25:
                order = self.doc(order=True, total=str(money(D(rng.randint(100, 3000)) * D('1.07'))), tax='0', when=when, customer=customer)
                order.tax = money(order.total * 7 / 107) if not exempt else D('0')
                order.save()
                orders.append(order)
            elif roll < 0.40 and orders:
                order = rng.choice(orders)
                OrderPayment.objects.create(order=order, amount=money(order.total * D(rng.choice(['0.25', '0.3333', '0.5']))),
                                            method=rng.choice(['cash', 'credit', 'account', 'check']), created_at=when)
            else:
                pre = D(rng.randint(5, 2000))
                tax = money(pre * D('0.07')) if not exempt else D('0')
                total = pre + tax
                split = rng.random()
                if split < 0.3:
                    account = money(total * D(rng.choice(['0.25', '0.5', '1'])))
                    payments = (('credit', str(total - account)), ('account', str(account)))
                elif split < 0.4:
                    payments = (('gift', str(money(total / 2))), ('cash', str(total - money(total / 2))))
                else:
                    payments = ((rng.choice(['cash', 'check', 'credit', 'debit']), str(total)),)
                self.doc(total=str(total), tax=str(tax), payments=payments, when=when, customer=customer,
                         imported=rng.random() < 0.5, tax_location='Exempt' if exempt else 'Springfield')
        for _ in range(6):
            a = date(2026, 8, 20) + timedelta(days=rng.randint(0, 30))
            b = a + timedelta(days=rng.randint(0, 20))
            self.both(a, b)
        whole, _ = self.both(date(2026, 8, 20), date(2026, 9, 30))
        first, _ = self.both(date(2026, 8, 20), date(2026, 9, 5))
        second, _ = self.both(date(2026, 9, 6), date(2026, 9, 30))
        self.assertEqual(first.tax + second.tax, whole.tax)
        self.assertEqual(whole.sections[1].tax, D('0'))
        for section in whole.sections:
            for row in section.rows:
                self.assertEqual(row.gross - row.discount, row.taxable + row.nontaxable, row)
                self.assertEqual(row.gross - row.discount + row.tax, row.total, row)

    def test_open_charges_follow_fifo_and_cap_at_the_balance(self):
        a = self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=3))
        b = self.doc(total='214', tax='14', payments=(('cash', '100'), ('account', '114')), when=WHEN - timedelta(days=2))
        self.doc(total='57', tax='0', payments=(('cash', '57'),), poa=True, when=WHEN - timedelta(days=1))
        from sales import accounts
        accounts.refresh(self.customer.pk)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.account_balance, D('164'))
        charges = tax_ledger.open_charges(self.customer)
        self.assertEqual(charges, [(('sale', a.pk), D('107'), D('50')), (('sale', b.pk), D('114'), D('114'))])
        owed = tax_ledger.owed_by_receipt(list(Document.objects.filter(pk__in=[a.pk, b.pk]).select_related('customer')))
        self.assertEqual(owed, {a.pk: D('50'), b.pk: D('114')})
        # A balance QuickBooks already settled (stored lower than the open lots) drops the oldest lots first.
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-100'))
        self.customer.refresh_from_db()
        self.assertEqual(tax_ledger.open_charges(self.customer), [(('sale', b.pk), D('114'), D('64'))])
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-164'))
        self.customer.refresh_from_db()
        self.assertEqual(tax_ledger.open_charges(self.customer), [])


class ReviewFixTests(LedgerFixture):
    """The three cross-review findings on 17bac4c, each with hand-computed cents."""

    def ledger(self):
        return tax_ledger.replay(tax_ledger.customer_events(self.customer.pk)).ledgers[self.customer.pk]

    def balance(self):
        from sales import accounts
        accounts.refresh(self.customer.pk)
        self.customer.refresh_from_db()
        return self.customer.account_balance

    def assert_conserved(self):
        """Open lots less unapplied credit is the customer's account balance (QB's stored number)."""
        ledger = self.ledger()
        self.assertEqual(sum((lot.amount for lot in ledger.lots if lot.key is not None), tax_ledger.ZERO)
                         - sum((c.amount for c in ledger.credits), tax_ledger.ZERO), self.balance())

    def poa_rows(self, st, section):
        return [r for r in st.sections[section].rows if r.kind == 'account']

    def refund(self, amount, customer=None, when=WHEN, imported=True):
        """A payment-on-account refund receipt ($amount back to the customer). Imported ones also bump the QB
        balance, since QuickBooks saw the refund re-charge the account."""
        customer = customer or self.customer
        doc = self.doc(total=f'-{amount}', tax='0', payments=(('cash', f'-{amount}'),), imported=imported, customer=customer,
                       lines=[('Payment On Account', f'-{amount}', 0)], when=when)
        if imported:
            Customer.objects.filter(pk=customer.pk).update(imported_account_balance=F('imported_account_balance') + D(amount))
            customer.refresh_from_db()
        return doc

    def rows(self, st, section, kind=None):
        return [r for r in st.sections[section].rows if kind is None or r.kind == kind]

    def cells(self, row):
        return (row.total, row.tax, row.taxable, row.nontaxable)

    def assert_rows_add_up(self, st):
        for section in st.sections:
            for row in section.rows:
                self.assertEqual(row.gross - row.discount, row.taxable + row.nontaxable, row)
                self.assertEqual(row.gross - row.discount + row.tax, row.total, row)

    def returned(self, original, pre, tax, when, customer=None, native=False, rate=7, untaxed=None, **kwargs):
        """A return of merchandise sold on `original` (`pre` before tax + `tax`, plus an `untaxed` line if given),
        credited to the account. Imported returns are QuickBooks' shape: a zero-total receipt whose Payment On
        Account line carries the value, lines linked by QuickBooks' receipt link (`returned_from_receipt`). App
        returns are what Accept Return saves (review 4): a negative receipt refunded by a negative Account tender,
        each line linked to the line it returns."""
        customer = customer or self.customer
        value = money(D(pre) + D(tax) + D(untaxed or 0))
        if native:
            doc = self.doc(total=f'-{value}', tax=f'-{tax}', payments=(('account', f'-{value}'),), customer=customer, when=when, lines=[], **kwargs)
        else:
            doc = self.doc(total='0', tax=f'-{tax}', payments=(), customer=customer, when=when, imported=True, **kwargs)
        lines = list(original.lines.exclude(item_name='Payment On Account').order_by('position'))
        parts = [(pre, rate if D(tax) else 0, lines[0])] + ([(untaxed, 0, lines[-1])] if untaxed else [])
        for pos, (amount, line_rate, line) in enumerate(parts, start=1):
            Line.objects.create(document=doc, position=pos, item_name='Returned', description='Returned', quantity=-1, price=money(amount),
                                tax_rate=line_rate, source_id=None if native else f'ret-{doc.pk}-{pos}',
                                returned_from_receipt=None if native else original, returned_from_line=line if native else None)
        if not native:
            Line.objects.create(document=doc, position=9, item_name='Payment On Account', description='Payment', quantity=1,
                                price=value, tax_rate=0, source_id=f'ret-poa-{doc.pk}')
        return doc

    def qb_balance(self, amount, customer=None):
        customer = customer or self.customer
        Customer.objects.filter(pk=customer.pk).update(imported_account_balance=D(amount))
        customer.refresh_from_db()

    def test_overpayment_is_prepaid_credit_that_funds_later_charges(self):
        # Reviewer's sequence (finding 1): the extra $107 paid on day 2 funds the taxable charge on day 3, so the
        # day-5 payment pays only the exempt charge and carries $0.00 tax (the old code released $6.54 again).
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        self.doc(payments=(('account', '107'),), when=d(1))
        self.assert_conserved()
        self.doc(total='214', tax='0', payments=(('cash', '214'),), poa=True, when=d(2))
        self.assert_conserved()
        funded = self.doc(payments=(('account', '107'),), when=d(3))
        self.assert_conserved()
        exempt = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=d(4))
        self.assert_conserved()
        self.doc(total='100', tax='0', payments=(('cash', '100'),), poa=True, when=d(5))
        self.assert_conserved()
        taxes = [self.both(d(n).date())[0].tax for n in range(1, 6)]
        self.assertEqual(taxes, [D('0'), D('14.00'), D('0'), D('0'), D('0.00')])
        self.assertEqual(sum(taxes), D('14.00'))                      # lifetime: the two taxable charges' $7 each, once
        day2, _ = self.both(d(2).date())
        self.assertEqual((day2.estimated_count, day2.estimated_total, day2.estimated_tax), (1, D('107.00'), D('7.00')))
        day5, dt5 = self.both(d(5).date())
        self.assertEqual(day5.estimated_count, 0)
        self.assertEqual(self.poa_rows(day5, 0), [])                   # nothing under Springfield
        [row] = self.poa_rows(day5, 1)                                 # the exempt charge's money, under Exempt
        self.assertEqual((row.total, row.tax, row.taxable, row.nontaxable), (D('100'), D('0'), D('0'), D('100')))
        self.assertEqual(dt5.totals['exempt'], D('100'))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))
        self.assertEqual(self.balance(), D('0'))
        self.assertEqual(tax_ledger.open_charges(self.customer), [])
        self.assertEqual(tax_ledger.owed_by_receipt([funded, exempt]), {})

    def test_refund_of_prepaid_credit_reverses_its_estimate(self):
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=2))
        self.doc(total='214', tax='0', payments=(('cash', '214'),), poa=True, when=WHEN - timedelta(days=1))
        self.doc(total='-107', tax='0', payments=(('cash', '-107'),), imported=True, lines=[('Payment On Account', '-107', 0)])
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('107'))   # QB: the refund re-charged it
        st, _ = self.both(date(2026, 9, 24))
        self.assertEqual((st.tax, st.estimated_count, st.estimated_total, st.estimated_tax), (D('-7.00'), 1, D('-107.00'), D('-7.00')))
        self.assertEqual((list(self.ledger().lots), list(self.ledger().credits)), ([], []))
        self.assert_conserved()
        # A refund after the credit funded a charge reopens that charge with its own tax, owed again.
        other = Customer.objects.create(source_id='re', first_name='Refunded', charge_account=True, imported_account_balance=D('107'))
        self.doc(payments=(('account', '107'),), customer=other, when=WHEN - timedelta(days=3))
        self.doc(total='214', tax='0', payments=(('cash', '214'),), poa=True, customer=other, when=WHEN - timedelta(days=2))
        funded = self.doc(payments=(('account', '107'),), customer=other, when=WHEN - timedelta(days=1))
        self.doc(total='-107', tax='0', payments=(('cash', '-107'),), imported=True, customer=other, lines=[('Payment On Account', '-107', 0)])
        st, _ = self.both(date(2026, 9, 24))
        [row] = [r for r in self.poa_rows(st, 0) if r.customer == other.display_name]
        self.assertEqual((row.total, row.tax, row.taxable, row.type_label), (D('-107'), D('-7.00'), D('-100.00'), 'Payment on account'))
        from sales import accounts
        accounts.refresh(other.pk)
        other.refresh_from_db()
        self.assertEqual(other.account_balance, D('107'))
        self.assertEqual(tax_ledger.open_charges(other), [(('sale', funded.pk), D('107'), D('107'))])

    def test_pre_backup_debt_is_not_credit(self):
        # QB says this customer owed $107 at the backup with no receipt behind it: the first payment settles that
        # unknown debt (estimated 7/107, flagged); it is NOT a prepayment, so the next charge stays open and owed.
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('107'))
        self.customer.refresh_from_db()
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=WHEN - timedelta(days=2))
        charged = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=WHEN - timedelta(days=1))
        self.assertEqual(self.balance(), D('100'))
        self.assertEqual(tax_ledger.open_charges(self.customer), [(('sale', charged.pk), D('100'), D('100'))])
        self.doc(total='100', tax='0', payments=(('cash', '100'),), poa=True)
        taxes = [self.both(date(2026, 9, 22 + n))[0].tax for n in range(3)]
        self.assertEqual(taxes, [D('7.00'), D('0'), D('0.00')])
        self.assertEqual(tax_ledger.open_charges(self.customer), [])
        self.assertEqual(self.balance(), D('0'))

    def test_payment_keeps_the_paid_charges_location(self):
        # Finding 2: the customer becomes Exempt, then pays an older Springfield charge: $7 under Springfield, $0 Exempt.
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))
        Customer.objects.filter(pk=self.customer.pk).update(tax_exempt=True, tax_location='Exempt')
        self.customer.refresh_from_db()
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, tax_location='Exempt')
        st, dt = self.both(date(2026, 9, 24))
        [row] = self.poa_rows(st, 0)
        self.assertEqual((row.total, row.tax, row.taxable, row.nontaxable), (D('107'), D('7.00'), D('100.00'), D('0.00')))
        self.assertEqual(self.poa_rows(st, 1), [])
        self.assertEqual((st.sections[0].tax, st.sections[1].tax, st.sections[1].taxable), (D('7.00'), D('0'), D('0')))
        self.assertEqual(dt.totals['exempt'], D('0'))

    def test_one_payment_covering_taxable_and_exempt_charges_is_split_by_charge(self):
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=2))
        self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=WHEN - timedelta(days=1))
        self.doc(total='207', tax='0', payments=(('check', '207'),), poa=True)
        st, dt = self.both(date(2026, 9, 24))
        [q] = self.poa_rows(st, 0)
        [e] = self.poa_rows(st, 1)
        self.assertEqual((q.total, q.tax, q.taxable, q.nontaxable), (D('107'), D('7.00'), D('100.00'), D('0.00')))
        self.assertEqual((e.total, e.tax, e.taxable, e.nontaxable), (D('100'), D('0'), D('0'), D('100')))
        self.assertEqual((st.payments_on_account, st.payments_on_account_tax), (D('207'), D('7.00')))
        self.assertEqual(dt.totals['exempt'], D('100'))
        self.assertEqual(dt.totals['received'], D('207'))

    def test_payment_carries_the_charges_taxable_and_nontaxable_bases(self):
        # Finding 3: $100 taxable at 8 % + $100 non-taxable charged ($208 incl. $8 tax); partial, closing and refunded
        # payments report the charged bases, not tax ÷ 7 %.
        charged = self.doc(total='208', tax='8', payments=(('account', '208'),), lines=[('Taxed', '100', 8), ('Untaxed', '100', 0)],
                           when=WHEN - timedelta(days=3))
        self.doc(total='104', tax='0', payments=(('cash', '104'),), poa=True, when=WHEN - timedelta(days=2))
        self.doc(total='104', tax='0', payments=(('cash', '104'),), poa=True, when=WHEN - timedelta(days=1))
        self.doc(total='-52', tax='0', payments=(('cash', '-52'),), imported=True, lines=[('Payment On Account', '-52', 0)])
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('52'))
        expected = {22: (D('104'), D('4.00'), D('50.00'), D('50.00')), 23: (D('104'), D('4.00'), D('50.00'), D('50.00')),
                    24: (D('-52'), D('-2.00'), D('-25.00'), D('-25.00'))}
        for day, want in expected.items():
            st, _ = self.both(date(2026, 9, day))
            [row] = self.poa_rows(st, 0)
            self.assertEqual((row.total, row.tax, row.taxable, row.nontaxable), want, day)
            self.assertEqual(row.gross, row.taxable + row.nontaxable)
            self.assertEqual(row.gross + row.tax, row.total)
        self.assertEqual(self.balance(), D('52'))
        self.assertEqual(tax_ledger.open_charges(self.customer), [(('sale', charged.pk), D('208'), D('52'))])
        # The exports carry the same bases.
        from io import BytesIO
        from openpyxl import load_workbook
        st, _ = self.both(date(2026, 9, 22))
        values = [list(r) for r in load_workbook(BytesIO(__import__('reports.sales_tax_exports', fromlist=['x']).xlsx_bytes(st))).active.values]
        self.assertTrue(any(50 in r and 4 in r and 104 in r for r in values), values)
        # Paid in full at once: $100 / $100 / $8 exactly.
        other = Customer.objects.create(source_id='mix', first_name='Mixed', charge_account=True)
        self.doc(total='208', tax='8', payments=(('account', '208'),), lines=[('Taxed', '100', 8), ('Untaxed', '100', 0)],
                 customer=other, when=WHEN - timedelta(days=10))
        self.doc(total='208', tax='0', payments=(('cash', '208'),), poa=True, customer=other, when=WHEN - timedelta(days=9))
        st, _ = self.both(date(2026, 9, 15))
        [row] = self.poa_rows(st, 0)
        self.assertEqual((row.total, row.tax, row.taxable, row.nontaxable), (D('208'), D('8.00'), D('100.00'), D('100.00')))


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class OnAccountStatusTests(LedgerFixture):
    def test_sales_history_and_customer_detail_show_owed_until_paid(self):
        from sales import accounts
        charged = self.doc(total='1080.65', tax='70.70', payments=(('credit', '500'), ('account', '580.65')), when=WHEN - timedelta(days=2))
        paid_in_full = self.doc(when=WHEN - timedelta(days=1))
        accounts.refresh(self.customer.pk)
        response = self.client.get('/pos/history/', {'period': 'all'})
        html = response.content.decode()
        self.assertIn('On account · $580.65 owed', html)
        self.assertEqual(html.count('On account ·'), 1)
        rows = self.client.get('/pos/history/', {'period': 'all', 'format': 'rows'}).json()['rows']
        by_id = {r['id']: r for r in rows}
        self.assertEqual(by_id[charged.pk]['cells'][2], 'On account · $580.65 owed')
        self.assertEqual(by_id[paid_in_full.pk]['cells'][2], 'Completed')
        detail = self.client.get('/customers/', {'customer': self.customer.pk})
        self.assertContains(detail, 'Open Charges')
        self.assertContains(detail, '$580.65')
        self.assertContains(detail, f'#{charged.number}')
        # Half paid: still owed shrinks; paid off: Completed again and the list empties.
        self.doc(total='300', tax='0', payments=(('check', '300'),), poa=True, when=WHEN)
        accounts.refresh(self.customer.pk)
        self.assertIn('On account · $280.65 owed', self.client.get('/pos/history/', {'period': 'all'}).content.decode())
        self.doc(total='280.65', tax='0', payments=(('cash', '280.65'),), poa=True, when=WHEN + timedelta(hours=1))
        accounts.refresh(self.customer.pk)
        html = self.client.get('/pos/history/', {'period': 'all'}).content.decode()
        self.assertNotIn('On account ·', html)
        detail = self.client.get('/customers/', {'customer': self.customer.pk})
        self.assertContains(detail, 'No open charges')
        self.assertNotContains(detail, '$280.65 owed')


class SecondReviewFixTests(ReviewFixTests):
    """The six findings of the second cross-review on 531dc51, each with hand-computed cents."""

    # 1. HIGH — a payment-on-account refund can reopen a charge that a remaining prepayment funds again; Sales Tax
    # must show that true-up like Daily Totals does.
    def test_poa_refunds_render_the_true_up_in_sales_tax(self):
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        self.doc(total='214', tax='0', payments=(('cash', '214'),), poa=True, when=d(1))             # prepaid: est 14.00
        exempt = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=d(2))
        self.refund('57', when=d(3))
        self.refund('157', when=d(4))
        self.assertEqual(self.balance(), D('100'))
        taxes = []
        for n in range(1, 5):
            st, dt = self.both(d(n).date())
            self.assert_rows_add_up(st)
            taxes.append(st.tax)
        # d1: 214 × 7/107 estimated. d2: the exempt charge is funded from the credit — the estimate on $100 (6.54) is
        # reversed under Springfield and $100 non-taxable lands under Exempt. d3: $57 of unapplied credit refunded
        # (−3.73). d4: the last $57 of credit (−3.73) and $100 that reopens the exempt charge (no tax).
        self.assertEqual(taxes, [D('14.00'), D('-6.54'), D('-3.73'), D('-3.73')])
        self.assertEqual(sum(taxes), D('0.00'))
        whole, dt = self.both(d(1).date(), d(4).date())
        self.assertEqual((whole.tax, dt.totals['tax'], dt.totals['received']), (D('0.00'), D('0.00'), D('0')))
        self.assertEqual((whole.sections[0].total, whole.sections[0].tax, whole.sections[0].taxable), (D('0.00'), D('0.00'), D('0.00')))
        self.assertEqual((whole.sections[1].total, whole.sections[1].tax, whole.sections[1].nontaxable), (D('0.00'), D('0'), D('0.00')))
        day2, dt2 = self.both(d(2).date())
        [q] = self.rows(day2, 0)
        self.assertEqual((q.type_label, *self.cells(q)), ('Prepayment applied · tax trued up', D('-100'), D('-6.54'), D('-93.46'), D('0.00')))
        e = {r.type_label: self.cells(r) for r in self.rows(day2, 1)}
        self.assertEqual(e['Prepayment applied · tax trued up'], (D('100'), D('0'), D('0'), D('100')))
        self.assertEqual(e['On account'], (D('0'), D('0'), D('0'), D('0')))
        self.assertEqual(dt2.totals['exempt'], D('100'))
        day4, _ = self.both(d(4).date())
        self.assertEqual({r.type_label: self.cells(r) for r in self.rows(day4, 0)},
                         {'Payment on account · estimated': (D('-57'), D('-3.73'), D('-53.27'), D('0.00'))})
        self.assertEqual({r.type_label: self.cells(r) for r in self.rows(day4, 1)},
                         {'Payment on account': (D('-100'), D('0'), D('0'), D('-100'))})
        self.assertEqual((day4.estimated_count, day4.estimated_total, day4.estimated_tax), (1, D('-57.00'), D('-3.73')))
        self.assertEqual(tax_ledger.open_charges(self.customer), [(('sale', exempt.pk), D('100'), D('100'))])
        self.assertEqual(tax_ledger.owed_by_receipt([exempt]), {exempt.pk: D('100')})
        self.assert_conserved()
        from io import BytesIO
        from openpyxl import load_workbook
        values = [list(r) for r in load_workbook(BytesIO(sales_tax_exports.xlsx_bytes(day2))).active.values]
        self.assertTrue(any(r[3] == 'Prepayment applied · tax trued up' and -6.54 in r for r in values if len(r) > 3), values)

    # 2. HIGH — a return credited to the account keeps the returned merchandise's own tax and location, wherever it is spent.
    def test_return_credit_spent_on_an_exempt_charge_reverses_the_tax_under_springfield(self):
        d = lambda n: WHEN - timedelta(days=n)   # noqa: E731
        self.doc(when=d(4))                                                                           # 107 cash, tax 7
        returned = self.doc(total='0', tax='-7', payments=(), imported=True, when=d(3),
                            lines=[('Returned', '-100', 7), ('Payment On Account', '107', 0)])     # credited to account
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-107'))
        self.customer.refresh_from_db()
        exempt = self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt', when=d(2))
        taxable = self.doc(payments=(('account', '107'),), when=d(1))
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True)
        taxes = [self.both(d(n).date())[0].tax for n in (4, 3, 2, 1, 0)]
        # The $107 cash turned out to buy exempt merchandise: its $7 is reversed the day the credit is spent; the
        # taxable charge's $7 arrives with the payment that pays it.
        self.assertEqual(taxes, [D('7.00'), D('0'), D('-7.00'), D('0'), D('7.00')])
        whole, dt = self.both(d(4).date(), d(0).date())
        self.assert_rows_add_up(whole)
        self.assertEqual(whole.tax, D('7.00'))
        self.assertEqual((whole.sections[0].total, whole.sections[0].tax, whole.sections[0].taxable), (D('107.00'), D('7.00'), D('100.00')))
        self.assertEqual((whole.sections[1].total, whole.sections[1].tax, whole.sections[1].nontaxable), (D('107.00'), D('0'), D('107.00')))
        self.assertEqual(dt.totals['exempt'], D('107'))
        day2, _ = self.both(d(2).date())
        [q] = self.rows(day2, 0)
        self.assertEqual((q.type_label, *self.cells(q)), ('Return credit applied · tax trued up', D('-107'), D('-7.00'), D('-100.00'), D('0.00')))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))
        self.assertFalse(hasattr(ledger, 'carry'))
        self.assertEqual(self.balance(), D('0'))
        self.assertEqual(tax_ledger.owed_by_receipt([exempt, taxable, returned]), {})

    def test_return_credit_spent_on_a_taxable_charge_needs_no_true_up_and_a_refund_reverses_it(self):
        self.doc(when=WHEN - timedelta(days=3))
        self.doc(total='0', tax='-7', payments=(), imported=True, when=WHEN - timedelta(days=2),
                 lines=[('Returned', '-100', 7), ('Payment On Account', '107', 0)])
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-107'))
        self.customer.refresh_from_db()
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))
        st, _ = self.both(date(2026, 9, 23))
        self.assertEqual((st.tax, [r.type_label for r in st.sections[0].rows]), (D('0'), ['On account']))
        whole, _ = self.both(date(2026, 9, 21), date(2026, 9, 23))
        self.assertEqual(whole.tax, D('7.00'))              # one taxable sale kept, one paid for in cash
        self.assertEqual(self.balance(), D('0'))
        # A customer with an unapplied return credit takes it in cash: the original sale's tax comes back that day.
        other = Customer.objects.create(source_id='rc', first_name='Refunded', charge_account=True)
        self.doc(customer=other, when=WHEN - timedelta(days=2))
        self.doc(total='0', tax='-7', payments=(), imported=True, customer=other, when=WHEN - timedelta(days=1),
                 lines=[('Returned', '-100', 7), ('Payment On Account', '107', 0)])
        Customer.objects.filter(pk=other.pk).update(imported_account_balance=D('-107'))
        other.refresh_from_db()
        self.refund('107', customer=other)
        st, _ = self.both(date(2026, 9, 24))
        [row] = [r for r in st.sections[0].rows if r.customer == other.display_name]
        self.assertEqual((row.type_label, *self.cells(row)), ('Payment on account', D('-107'), D('-7.00'), D('-100.00'), D('0.00')))
        self.assertEqual(st.estimated_count, 0)
        from sales import accounts
        accounts.refresh(other.pk)
        other.refresh_from_db()
        self.assertEqual(other.account_balance, D('0'))
        ledger = tax_ledger.replay(tax_ledger.customer_events(other.pk)).ledgers[other.pk]
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))

    # 3. MEDIUM — several refunds reopening one receipt: one row owing the whole amount.
    def test_repeated_refunds_reopen_one_receipt_once_for_the_full_amount(self):
        charged = self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=3))
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=WHEN - timedelta(days=2))
        self.refund('30', when=WHEN - timedelta(days=1))
        self.refund('20')
        self.assertEqual(self.balance(), D('50'))
        self.assertEqual(tax_ledger.open_charges(self.customer), [(('sale', charged.pk), D('107'), D('50'))])
        self.assertEqual(tax_ledger.owed_by_receipt([charged]), {charged.pk: D('50')})
        from customers import detail
        rows = detail.open_charges(self.customer)
        self.assertEqual((rows['count'], rows['total'], [(r['charged'], r['owed']) for r in rows['rows']]), (1, D('50'), [(D('107'), D('50'))]))
        taxes = [self.both(date(2026, 9, 21 + n))[0].tax for n in range(4)]
        self.assertEqual(taxes, [D('0'), D('7.00'), D('-1.96'), D('-1.31')])    # 30 × 7/107; 20 × 5.04/77
        self.assertEqual(sum(taxes), D('3.73'))                                 # 57 still paid for
        # Refund after a partial payment, then the balance cap: still one row, the full amount.
        other = Customer.objects.create(source_id='pp', first_name='Partial', charge_account=True)
        charged2 = self.doc(payments=(('account', '107'),), customer=other, when=WHEN - timedelta(days=3))
        self.doc(total='50', tax='0', payments=(('cash', '50'),), poa=True, customer=other, when=WHEN - timedelta(days=2))
        self.refund('20', customer=other, when=WHEN - timedelta(days=1))
        self.refund('30', customer=other)
        from sales import accounts
        accounts.refresh(other.pk)
        other.refresh_from_db()
        self.assertEqual(other.account_balance, D('107'))
        self.assertEqual(tax_ledger.open_charges(other), [(('sale', charged2.pk), D('107'), D('107'))])
        Customer.objects.filter(pk=other.pk).update(imported_account_balance=F('imported_account_balance') - 40)
        other.refresh_from_db()
        self.assertEqual(tax_ledger.open_charges(other), [(('sale', charged2.pk), D('107'), D('67'))])
        self.assertEqual(detail.open_charges(other)['count'], 1)

    # 4. MEDIUM — a true-up moves the money between locations and carries the real bases, not just a tax number.
    def test_true_up_moves_the_prepayment_to_the_charges_location_and_bases(self):
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=datetime(2026, 8, 31, 12, tzinfo=NY))
        self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt', when=datetime(2026, 9, 1, 12, tzinfo=NY))
        aug, _ = self.both(date(2026, 8, 31))
        sep, dt = self.both(date(2026, 9, 1))
        whole, _ = self.both(date(2026, 8, 31), date(2026, 9, 1))
        for st in (aug, sep, whole):
            self.assert_rows_add_up(st)
        self.assertEqual([(s.total, s.tax, s.taxable, s.nontaxable) for s in aug.sections],
                         [(D('107'), D('7.00'), D('100.00'), D('0.00')), (D('0'), D('0'), D('0'), D('0'))])
        self.assertEqual([(s.total, s.tax, s.taxable, s.nontaxable) for s in sep.sections],
                         [(D('-107'), D('-7.00'), D('-100.00'), D('0.00')), (D('107'), D('0'), D('0'), D('107'))])
        self.assertEqual([(s.total, s.tax, s.taxable, s.nontaxable) for s in whole.sections],
                         [(D('0'), D('0.00'), D('0.00'), D('0.00')), (D('107'), D('0'), D('0'), D('107'))])
        self.assertEqual((sep.tax, sep.total, sep.reconciled_tax, dt.totals['tax'], dt.totals['received'], dt.totals['exempt']),
                         (D('-7.00'), D('0'), D('-7.00'), D('-7.00'), D('0'), D('107')))
        self.assertEqual([r.type_label for r in self.rows(sep, 0)], ['Prepayment applied · tax trued up'])
        # Prepayment meets a mixed-rate charge: 208 at the 7/107 estimate (13.61 tax, 194.39 taxable) becomes
        # $100 taxable at 8 % + $100 non-taxable — all under Springfield, with Total 0 and the bases corrected.
        other = Customer.objects.create(source_id='mx', first_name='Mixed', charge_account=True)
        self.doc(total='208', tax='0', payments=(('cash', '208'),), poa=True, customer=other, when=WHEN - timedelta(days=1))
        self.doc(total='208', tax='8', payments=(('account', '208'),), lines=[('Taxed', '100', 8), ('Untaxed', '100', 0)], customer=other)
        day, _ = self.both(date(2026, 9, 24))
        [row] = [r for r in self.rows(day, 0, 'account') if r.customer == other.display_name]
        self.assertEqual((row.type_label, row.gross, row.taxable, row.nontaxable, row.tax, row.total),
                         ('Prepayment applied · tax trued up', D('5.61'), D('-94.39'), D('100.00'), D('-5.61'), D('0')))
        lifetime, _ = self.both(date(2026, 9, 23), date(2026, 9, 24))
        mixed = [r for s in lifetime.sections for r in s.rows if r.customer == other.display_name]
        self.assertEqual((sum(r.total for r in mixed), sum(r.tax for r in mixed), sum(r.taxable for r in mixed), sum(r.nontaxable for r in mixed)),
                         (D('208'), D('8.00'), D('100.00'), D('100.00')))

    # 5. MEDIUM — a QB credit from before the data funds the first charges; a QB balance below the receipts is not
    # turned into money for later charges beyond what QuickBooks itself shows as credit.
    def test_opening_qb_credit_funds_the_first_charge_without_tax(self):
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-107'))
        self.customer.refresh_from_db()
        funded = self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=2))
        exempt = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=WHEN - timedelta(days=1))
        self.doc(total='100', tax='0', payments=(('cash', '100'),), poa=True)
        taxes = [self.both(date(2026, 9, 22 + n))[0].tax for n in range(3)]
        self.assertEqual(taxes, [D('0'), D('0'), D('0')])          # the payment pays the exempt charge; the taxable one was prepaid before the data
        self.assertEqual(self.balance(), D('0'))
        ledger = self.ledger()
        self.assertEqual((ledger.balance(), list(ledger.lots), list(ledger.credits)), (D('0'), [], []))
        self.assertEqual(tax_ledger.open_charges(self.customer), [])
        self.assertEqual(tax_ledger.owed_by_receipt([funded, exempt]), {})
        st, _ = self.both(date(2026, 9, 24))
        [row] = self.rows(st, 1, 'account')
        self.assertEqual((row.type_label, *self.cells(row)), ('Payment on account', D('100'), D('0'), D('0'), D('100')))
        self.assertEqual(st.estimated_count, 0)

    def test_opening_qb_credit_refunded_is_estimated_and_positive_debt_still_seeds(self):
        Customer.objects.filter(pk=self.customer.pk).update(imported_account_balance=D('-107'))
        self.customer.refresh_from_db()
        self.refund('107')                       # QB: the credit paid out, balance back to 0
        st, _ = self.both(date(2026, 9, 24))
        [row] = self.rows(st, 0, 'account')
        self.assertEqual((row.type_label, *self.cells(row)), ('Payment on account · estimated', D('-107'), D('-7.00'), D('-100.00'), D('0.00')))
        self.assertEqual((st.estimated_count, st.estimated_tax), (1, D('-7.00')))
        self.assertEqual(self.balance(), D('0'))
        self.assert_conserved()
        # QB balance above the receipts: debt from before the data, settled first (unchanged).
        other = Customer.objects.create(source_id='db', first_name='Debt', charge_account=True, imported_account_balance=D('50'))
        charged = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', customer=other, when=WHEN - timedelta(days=1))
        self.doc(total='50', tax='0', payments=(('cash', '50'),), poa=True, customer=other)
        st, _ = self.both(date(2026, 9, 24))
        [row] = [r for r in self.rows(st, 0, 'account') if r.customer == other.display_name]
        self.assertEqual((row.type_label, row.tax), ('Payment on account · estimated', D('3.27')))
        from sales import accounts
        accounts.refresh(other.pk)
        other.refresh_from_db()
        self.assertEqual(other.account_balance, D('100'))
        self.assertEqual(tax_ledger.open_charges(other), [(('sale', charged.pk), D('100'), D('100'))])
        # QB balance below the receipts but not negative: QuickBooks settled part of the history — the oldest charges
        # are funded as if prepaid before the data, nothing is left to spend on later charges.
        third = Customer.objects.create(source_id='adj', first_name='Adjusted', charge_account=True, imported_account_balance=D('0'))
        old = self.doc(payments=(('account', '107'),), customer=third, imported=True, when=WHEN - timedelta(days=3))
        later = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', customer=third, when=WHEN - timedelta(days=1))
        self.doc(total='100', tax='0', payments=(('cash', '100'),), poa=True, customer=third)
        st, _ = self.both(date(2026, 9, 24))
        [row] = [r for r in self.rows(st, 1, 'account') if r.customer == third.display_name]
        self.assertEqual(self.cells(row), (D('100'), D('0'), D('0'), D('100')))
        self.assertEqual(tax_ledger.owed_by_receipt([old, later]), {})
        ledger = tax_ledger.replay(tax_ledger.customer_events(third.pk)).ledgers[third.pk]
        self.assertEqual((ledger.balance(), list(ledger.credits)), (D('0'), []))

    # 6. LOW — the detailed PDF wraps the Type cell so a long label can't overprint the money columns.
    def test_detailed_pdf_wraps_the_type_label(self):
        from unittest import mock
        from reportlab.platypus import Paragraph, Table
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=WHEN - timedelta(days=1))
        self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt')
        st = sales_tax.build(period(date(2026, 9, 24)), detailed=True)
        with mock.patch('reportlab.platypus.Table', wraps=Table) as tables:
            pdf = sales_tax_exports.pdf_bytes(st)
        labels = [row[3] for call in tables.call_args_list for row in call.args[0][1:] if len(row) == 10 and not isinstance(row[0], Paragraph)]
        self.assertTrue(labels)
        self.assertTrue(all(isinstance(cell, Paragraph) for cell in labels), labels)
        self.assertIn('Prepayment applied', ' '.join(cell.text for cell in labels))
        self.assertGreater(len(pdf), 1000)

    def test_random_sequences_conserve_tax_credit_and_what_is_owed(self):
        """Random histories per customer — an imported era (charges, payments, refunds — one may come before any
        history — returns credited with and without QuickBooks' receipt link, and a QuickBooks balance settled off the
        books at the backup) then an app era (charges, payments, refunds, returns in Accept Return's own shape: a
        negative receipt refunded to the account, lines linked to the lines they return — review 4) — checked
        against `DayReference`: Sales Tax and Daily Totals must put every cent of tax and money on the same DAY and
        location the reference does (review 3), not only add up over the lifetime. Then, once every balance is
        settled, lifetime tax across both reports equals the recorded tax of every receipt (less what QuickBooks
        accounted for before the data), each section's Total is the receipts' money by location, the ledgers hold
        nothing, and before settling, owes exactly the balance."""
        from sales import accounts
        from customers import detail
        rng = random.Random(int(os.environ.get('FS_PROPERTY_SEED', '2026')))     # sweep seeds: FS_PROPERTY_SEED=n
        customers = [Customer.objects.create(source_id=f'p{i}', first_name=f'P{i}', charge_account=True,
                                             tax_location='Exempt' if i == 1 else '', tax_exempt=i == 1) for i in range(4)]
        start = datetime(2026, 9, 1, 9, tzinfo=NY)
        refundable = {c.pk: ZERO for c in customers}
        qb_balance = {c.pk: ZERO for c in customers}      # what QuickBooks would store: the imported receipts' net
        extra = {c.pk: ZERO for c in customers}           # settled by QuickBooks off the books at the backup
        receipts = {c.pk: [] for c in customers}          # charged receipts not yet returned
        expected = {DEFAULT_LOCATION: [ZERO, ZERO], EXEMPT: [ZERO, ZERO]}      # [money (new sale), tax] by location
        log = []                                          # (customer pk, imported?, reference call, args) in event order
        day = timezone.localdate

        def sync(customer):
            Customer.objects.filter(pk=customer.pk).update(imported_account_balance=qb_balance[customer.pk] - extra[customer.pk])
            accounts.refresh(customer.pk)
            customer.refresh_from_db()

        def loc_of(customer):
            return EXEMPT if customer.tax_exempt else DEFAULT_LOCATION

        def charge(customer, when, imported):
            exempt, loc = customer.tax_exempt, loc_of(customer)
            rate = rng.choice([7, 7, 8])
            taxed, untaxed = D(rng.randint(1, 400)), rng.choice([ZERO, D(rng.randint(1, 200))])
            tax = money(taxed * rate / 100) if not exempt else ZERO
            total = taxed + untaxed + tax
            account = rng.choice([total, money(total / 2), money(total / 3)])
            payments = (('account', str(account)),) if account == total else (('credit', str(total - account)), ('account', str(account)))
            doc = self.doc(total=str(total), tax=str(tax), payments=payments, customer=customer, when=when, tax_location=loc, imported=imported,
                           lines=[('Taxed', str(taxed), rate if not exempt else 0)] + ([('Untaxed', str(untaxed), 0)] if untaxed else []))
            expected[loc][0] += total
            expected[loc][1] += tax
            if imported:
                qb_balance[customer.pk] += account
            receipts[customer.pk].append((doc, taxed, untaxed, tax, rate if not exempt else 0, loc))
            log.append((customer.pk, imported, 'sale', (day(when), customer.pk, total, tax, total - account, account, loc, ('sale', doc.pk), imported)))

        def payment(customer, when, imported):
            amount = D(rng.randint(1, 600))
            self.doc(total=str(amount), tax='0', payments=((rng.choice(['cash', 'check']), str(amount)),), poa=True, customer=customer,
                     when=when, tax_location=loc_of(customer), imported=imported)
            refundable[customer.pk] += amount
            if imported:
                qb_balance[customer.pk] -= amount
            log.append((customer.pk, imported, 'poa', (day(when), customer.pk, amount)))

        def refund(customer, when, amount):
            self.doc(total=f'-{amount}', tax='0', payments=(('cash', f'-{amount}'),), customer=customer, when=when, tax_location=loc_of(customer),
                     imported=True, lines=[('Payment On Account', f'-{amount}', 0)])
            refundable[customer.pk] = max(refundable[customer.pk] - amount, ZERO)
            qb_balance[customer.pk] += amount
            log.append((customer.pk, True, 'refund', (day(when), customer.pk, amount)))

        def return_unlinked(customer, when):
            exempt, loc = customer.tax_exempt, loc_of(customer)
            pre = D(rng.randint(1, 300))
            tax = money(pre * 7 / 100) if not exempt else ZERO
            self.doc(total='0', tax=f'-{tax}', payments=(), customer=customer, when=when, tax_location=loc, imported=True,
                     lines=[('Returned', f'-{pre}', 7 if not exempt else 0), ('Payment On Account', str(pre + tax), 0)])
            refundable[customer.pk] += pre + tax
            qb_balance[customer.pk] -= pre + tax
            expected[loc][0] -= pre + tax
            expected[loc][1] -= tax
            log.append((customer.pk, True, 'return_credit', (day(when), customer.pk, pre + tax, tax, loc, None)))

        def return_linked(customer, when, native):
            pool = receipts[customer.pk]
            if not pool:
                return
            # Half the time the newest charge comes back, so an older charge is often still open under it (the
            # review-4 shape: a return must come off its own charge, never fund the older one).
            doc, taxed, untaxed, tax, rate, loc = pool.pop(len(pool) - 1 if rng.random() < 0.5 else rng.randrange(len(pool)))     # QuickBooks' receipt-linked return keeps the original's location and tax
            what = rng.choice(['all', 'taxed', 'untaxed'] if untaxed else ['all'])
            pre, rtax = (untaxed, ZERO) if what == 'untaxed' else (taxed, tax)
            with_untaxed = untaxed if what == 'all' and untaxed else None
            self.returned(doc, str(pre), str(rtax), when, customer=customer, native=native, rate=rate, untaxed=with_untaxed and str(with_untaxed),
                          tax_location=loc)
            value = pre + rtax + (with_untaxed or ZERO)
            refundable[customer.pk] += value
            if not native:
                qb_balance[customer.pk] -= value
            expected[loc][0] -= value
            expected[loc][1] -= rtax
            log.append((customer.pk, not native, 'return_credit', (day(when), customer.pk, value, rtax, loc, ('sale', doc.pk))))

        def flip(customer):
            customer.tax_exempt = not customer.tax_exempt
            customer.tax_location = EXEMPT if customer.tax_exempt else DEFAULT_LOCATION
            customer.save()

        when = start
        for i in range(70):
            when += timedelta(hours=rng.randint(1, 9))
            customer = rng.choice(customers)
            imported = i < 35                           # the first half of the history is QuickBooks', the rest the app's
            roll = rng.random()
            if roll < 0.30:
                charge(customer, when, imported)
            elif roll < 0.52:
                payment(customer, when, imported)
            elif roll < 0.62 and refundable[customer.pk] >= 1 and (imported or not extra[customer.pk]):
                refund(customer, when, D(rng.randint(1, int(refundable[customer.pk]))))
            elif roll < 0.65 and imported and not any(entry[0] == customer.pk for entry in log):
                refund(customer, when, D(rng.randint(1, 200)))                    # before any history (finding 3)
            elif roll < 0.75 and imported:
                return_unlinked(customer, when)
            elif roll < 0.88:
                return_linked(customer, when, native=not imported)
            else:
                flip(customer)
            if i == 34:
                # The backup moment: QuickBooks' balance may sit below the imported receipts (finding 2).
                for c in customers:
                    if rng.random() < 0.5:
                        extra[c.pk] = D(rng.randint(1, 300))
                        last = max((n for n, entry in enumerate(log) if entry[0] == c.pk and entry[1]), default=-1)
                        log.insert(last + 1, (c.pk, False, 'reconcile', (day(when) if last >= 0 else start.date(), c.pk, extra[c.pk])))
        # Before settling: the ledger's open charges less credits are the balance; what says is owed is the open
        # receipts up to the balance (debt from before the data has no receipt to show), one row per charge, never
        # more than charged.
        for customer in customers:
            sync(customer)
            ledger = tax_ledger.replay(tax_ledger.customer_events(customer.pk)).ledgers[customer.pk]
            self.assertEqual(ledger.balance(), customer.account_balance, customer)
            keyed = sum((lot.amount for lot in ledger.lots if lot.key is not None), ZERO)
            charges = tax_ledger.open_charges(customer)
            self.assertEqual(sum((owed for _, _, owed in charges), ZERO), max(min(customer.account_balance, keyed), ZERO), customer)
            self.assertEqual(len({key for key, _, _ in charges}), len(charges))
            for _, charged, owed in charges:
                self.assertLessEqual(owed, charged)
            self.assertEqual(detail.open_charges(customer)['total'], max(min(customer.account_balance, keyed), ZERO))
        # Settle every account: pay what is owed; a customer holding credit spends it on one more (untaxed) charge.
        when += timedelta(days=1)
        for customer in customers:
            balance = customer.account_balance
            if balance > 0:
                self.doc(total=str(balance), tax='0', payments=(('cash', str(balance)),), poa=True, customer=customer, when=when, tax_location=loc_of(customer))
                log.append((customer.pk, False, 'poa', (day(when), customer.pk, balance)))
            elif balance < 0:
                doc = self.doc(total=str(-balance), tax='0', payments=(('account', str(-balance)),), customer=customer, when=when,
                               tax_location=loc_of(customer), lines=[('Untaxed', str(-balance), 0)])
                expected[loc_of(customer)][0] += -balance
                log.append((customer.pk, False, 'sale', (day(when), customer.pk, -balance, ZERO, ZERO, -balance, loc_of(customer), ('sale', doc.pk), False)))
            sync(customer)
            self.assertEqual(customer.account_balance, ZERO)
            ledger = tax_ledger.replay(tax_ledger.customer_events(customer.pk)).ledgers[customer.pk]
            self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []), customer)
        # The reference model, run with the customers' final Exempt flags (the ledger estimates with the current one).
        ref = DayReference()
        final_exempt = {c.pk: c.tax_exempt for c in customers}
        if os.environ.get('FS_PROPERTY_VERBOSE'):
            from collections import Counter
            print('events:', dict(Counter(f"{call}{'' if imported_ else '(app)'}{' linked' if call == 'return_credit' and args[-1] else ''}"
                                          for _, imported_, call, args in log)), 'settled off the books:', {k: str(v) for k, v in extra.items() if v})
        for pk, _, call, args in log:
            if call in ('poa', 'refund', 'reconcile'):
                args = args + (final_exempt[pk],)
            getattr(ref, call)(*args)
        first, last = start.date(), day(when)
        days = []
        d = first
        while d <= last:
            st, dt = self.both(d)
            days.append((st, dt))
            for section in st.sections:
                self.assertEqual((section.tax, section.total), (ref.tax[(d, section.location)], ref.money[(d, section.location)]), (d, section.location))
            self.assertEqual((dt.totals['received'], dt.totals['exempt']), (ref.received[d], ref.money[(d, EXEMPT)]), d)
            d += timedelta(days=1)
        whole, dt = self.both(first, last)
        self.assert_rows_add_up(whole)
        for section in whole.sections:
            money_, tax = expected[section.location]
            before_money, before_tax = ref.before[section.location]
            self.assertEqual((section.total, section.tax), (money_ - before_money, tax - before_tax), section.location)
        self.assertEqual(dt.totals['exempt'], whole.sections[1].total)
        for i, section in enumerate(whole.sections):
            for name in ('total', 'tax', 'taxable', 'nontaxable'):
                self.assertEqual(sum((getattr(st.sections[i], name) for st, _ in days), ZERO), getattr(section, name), (section.location, name))
        self.assertEqual(sum((d.totals['tax'] for _, d in days), ZERO), dt.totals['tax'])
        self.assertEqual(sum((d.totals['exempt'] for _, d in days), ZERO), dt.totals['exempt'])


class ThirdReviewFixTests(ReviewFixTests):
    """The three findings of the third cross-review on dce3f7f: hand-computed cents AND the day each tax row lands on
    (lifetime totals alone passed on the broken code)."""

    def account_rows(self, st):
        return [(s.location, r.type_label, *self.cells(r)) for s in st.sections for r in s.rows if r.kind == 'account']

    # 1. HIGH — a return of merchandise never paid for cancels the charge; no tax was collected, so none is reversed
    # (the old code reversed $7 on 8/31 and collected it again on 9/1, across the NYS quarter boundary).
    def test_return_of_unpaid_merchandise_cancels_the_charge_without_a_tax_row(self):
        aug = lambda day: datetime(2026, 8, day, 12, tzinfo=NY)   # noqa: E731
        exempt = self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt', imported=True, when=aug(29))
        taxable = self.doc(payments=(('account', '107'),), imported=True, when=aug(30))
        returned = self.returned(taxable, '100', '7', aug(31))
        self.qb_balance('107')                                              # QuickBooks: only the exempt charge is owed
        self.assertEqual(tax_ledger.owed_by_receipt([exempt, taxable]), {exempt.pk: D('107')})
        self.assert_conserved()
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=datetime(2026, 9, 1, 12, tzinfo=NY))
        days = [self.both(d) for d in (date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1))]
        self.assertEqual([st.tax for st, _ in days], [D('0'), D('0'), D('0'), D('0')])
        self.assertEqual([dt.totals['received'] for _, dt in days], [D('0'), D('0'), D('0'), D('107')])
        self.assertEqual([dt.totals['exempt'] for _, dt in days], [D('0'), D('0'), D('0'), D('107')])
        aug31, _ = days[2]
        self.assert_rows_add_up(aug31)
        self.assertEqual((self.account_rows(aug31), aug31.reconciled_tax), ([], D('0')))
        sep1, _ = days[3]
        self.assertEqual(self.account_rows(sep1), [(EXEMPT, 'Payment on account', D('107'), D('0'), D('0'), D('107'))])
        self.assertEqual(sep1.estimated_count, 0)
        q2, _ = self.both(date(2026, 6, 1), date(2026, 8, 31))
        q3, _ = self.both(date(2026, 9, 1), date(2026, 11, 30))
        self.assertEqual((q2.tax, q3.tax), (D('0'), D('0')))
        self.assertEqual((q2.sections[0].total, q3.sections[1].total), (D('0'), D('107')))
        self.assertEqual(self.balance(), D('0'))
        self.assertEqual(tax_ledger.owed_by_receipt([exempt, taxable, returned]), {})
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))

    def test_return_of_a_part_paid_charge_cancels_what_is_owed_and_credits_the_paid_part(self):
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        taxable = self.doc(payments=(('account', '107'),), imported=True, when=d(1))
        self.doc(total='50', tax='0', payments=(('cash', '50'),), poa=True, imported=True, when=d(2))    # releases 50 × 7/107 = 3.27
        self.returned(taxable, '100', '7', d(3))          # $57 still owed comes off (its 3.73 never collected); $50 credit carries the 3.27
        self.qb_balance('-50')
        self.assert_conserved()
        exempt = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=d(4))   # credit funds $50 of it
        self.assertEqual(self.balance(), D('50'))
        self.assertEqual(tax_ledger.owed_by_receipt([taxable, exempt]), {exempt.pk: D('50')})
        self.doc(total='50', tax='0', payments=(('cash', '50'),), poa=True, when=d(5))
        days = [self.both(d(n).date()) for n in range(1, 6)]
        self.assertEqual([st.tax for st, _ in days], [D('0'), D('3.27'), D('0'), D('-3.27'), D('0')])
        self.assertEqual(self.account_rows(days[2][0]), [])
        day4, dt4 = days[3]
        self.assert_rows_add_up(day4)
        self.assertEqual(self.account_rows(day4), [(DEFAULT_LOCATION, 'Return credit applied · tax trued up', D('-50'), D('-3.27'), D('-46.73'), D('0.00')),
                                                   (EXEMPT, 'Return credit applied · tax trued up', D('50'), D('0'), D('0'), D('50'))])
        self.assertEqual(dt4.totals['exempt'], D('50'))
        self.assertEqual(self.account_rows(days[4][0]), [(EXEMPT, 'Payment on account', D('50'), D('0'), D('0'), D('50'))])
        whole, dt = self.both(d(1).date(), d(5).date())
        self.assertEqual([(s.total, s.tax) for s in whole.sections], [(D('0'), D('0.00')), (D('100'), D('0'))])
        self.assertEqual((dt.totals['received'], dt.totals['exempt'], self.balance()), (D('100'), D('100'), D('0')))

    def test_return_of_an_unpaid_exempt_charge_leaves_the_taxable_one_owed(self):
        # Inverse locations: the old code let the exempt return credit "fund" the older taxable charge and reported
        # its $7 on the return day, with no money received.
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        taxable = self.doc(payments=(('account', '107'),), imported=True, when=d(1))
        exempt = self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt', imported=True, when=d(2))
        self.returned(exempt, '107', '0', d(3), tax_location='Exempt')
        self.qb_balance('107')
        self.assertEqual(tax_ledger.owed_by_receipt([taxable, exempt]), {taxable.pk: D('107')})
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=d(4))
        days = [self.both(d(n).date()) for n in range(1, 5)]
        self.assertEqual([st.tax for st, _ in days], [D('0'), D('0'), D('0'), D('7.00')])
        self.assertEqual([dt.totals['exempt'] for _, dt in days], [D('0'), D('0'), D('0'), D('0')])
        self.assertEqual(self.account_rows(days[2][0]), [])
        self.assertEqual(self.account_rows(days[3][0]), [(DEFAULT_LOCATION, 'Payment on account', D('107'), D('7.00'), D('100.00'), D('0.00'))])
        self.assertEqual((self.balance(), tax_ledger.owed_by_receipt([taxable, exempt])), (D('0'), {}))

    def test_return_of_the_taxed_item_from_a_part_paid_mixed_charge_reverses_only_the_collected_tax(self):
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        mixed = self.doc(total='207', tax='7', payments=(('account', '207'),), imported=True, when=d(1),
                         lines=[('Taxed', '100', 7), ('Untaxed', '100', 0)])
        self.doc(total='100', tax='0', payments=(('cash', '100'),), poa=True, imported=True, when=d(2))   # releases 100 × 7/207 = 3.38
        self.returned(mixed, '100', '7', d(3))                   # the taxed item comes back; the paid-for untaxed one is kept
        self.qb_balance('0')
        days = [self.both(d(n).date()) for n in range(1, 4)]
        # The $107 still owed comes off the charge (with the 3.62 of tax it carried); the 3.38 collected on the $100
        # cash turns out to be tax on returned goods, and with no credit left to carry it, it is reversed the same day.
        self.assertEqual([st.tax for st, _ in days], [D('0'), D('3.38'), D('-3.38')])
        day3, _ = days[2]
        self.assert_rows_add_up(day3)
        self.assertEqual(self.account_rows(day3), [(DEFAULT_LOCATION, 'Return against charge · tax trued up', D('0'), D('-3.38'), D('-48.31'), D('51.69'))])
        self.assertEqual((self.balance(), tax_ledger.owed_by_receipt([mixed])), (D('0'), {}))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))
        whole, _ = self.both(d(1).date(), d(3).date())
        self.assertEqual((whole.tax, whole.sections[0].total), (D('0.00'), D('100')))

    def test_app_return_of_a_charged_receipt_is_linked_by_its_line(self):
        exempt = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=WHEN - timedelta(days=2))
        charged = self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))
        self.assertEqual(self.balance(), D('207'))
        self.assertEqual(tax_ledger.owed_by_receipt([exempt, charged]), {exempt.pk: D('100'), charged.pk: D('107')})
        self.returned(charged, '100', '7', WHEN, native=True)      # comes off its own charge, not the older exempt one
        taxes = [self.both(date(2026, 9, 22 + n))[0].tax for n in range(3)]
        self.assertEqual(taxes, [D('0'), D('0'), D('0')])
        self.assertEqual(self.account_rows(self.both(date(2026, 9, 24))[0]), [])
        self.assertEqual((self.balance(), tax_ledger.owed_by_receipt([exempt, charged])), (D('100'), {exempt.pk: D('100')}))
        ledger = self.ledger()
        self.assertEqual(([(lot.key, lot.amount) for lot in ledger.lots], list(ledger.credits)), ([(('sale', exempt.pk), D('100'))], []))

    # 2. MEDIUM — QuickBooks' balance below the imported receipts is an adjustment at the backup moment, not money
    # that funded the first charges: it never changes which charge a real payment inside the data paid.
    def test_unexplained_qb_settlement_does_not_change_which_charge_a_real_payment_paid(self):
        d = lambda n: WHEN - timedelta(days=n)   # noqa: E731
        exempt = self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt', imported=True, when=d(3))
        taxable = self.doc(payments=(('account', '107'),), imported=True, when=d(2))
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, imported=True, when=d(1))
        self.qb_balance('0')          # QuickBooks settled the other charge somehow — no receipt in the data says how or when
        days = [self.both(d(n).date()) for n in (3, 2, 1)]
        self.assertEqual([st.tax for st, _ in days], [D('0'), D('0'), D('0')])    # the payment paid the oldest charge: the exempt one
        day1, dt1 = days[2]
        self.assertEqual(self.account_rows(day1), [(EXEMPT, 'Payment on account', D('107'), D('0'), D('0'), D('107'))])
        self.assertEqual((dt1.totals['exempt'], day1.estimated_count), (D('107'), 0))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))
        self.assertEqual([(s.key, s.amount, s.tax) for s in ledger.settled], [(('sale', taxable.pk), D('107'), D('7'))])   # flagged, no tax
        self.assertEqual((self.balance(), tax_ledger.owed_by_receipt([exempt, taxable])), (D('0'), {}))

    def test_qb_credit_beyond_the_imported_charges_is_held_credit_after_settling_them(self):
        exempt = self.doc(total='107', tax='0', payments=(('account', '107'),), tax_location='Exempt', imported=True, when=WHEN - timedelta(days=2))
        self.qb_balance('-107')       # QuickBooks: that charge is settled and the customer still holds $107
        self.assertEqual(tax_ledger.owed_by_receipt([exempt]), {})
        funded = self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))      # the held credit funds it silently
        self.doc(total='50', tax='0', payments=(('cash', '50'),), poa=True)                   # beyond every charge: a prepayment
        taxes = [self.both(date(2026, 9, 22 + n))[0].tax for n in range(3)]
        self.assertEqual(taxes, [D('0'), D('0'), D('3.27')])
        st, _ = self.both(date(2026, 9, 24))
        self.assertEqual(self.account_rows(st), [(DEFAULT_LOCATION, 'Payment on account · estimated', D('50'), D('3.27'), D('46.73'), D('0.00'))])
        self.assertEqual(self.balance(), D('-50'))
        self.assert_conserved()
        ledger = self.ledger()
        self.assertEqual(([(s.key, s.amount) for s in ledger.settled], list(ledger.lots), [(c.kind, c.amount) for c in ledger.credits]),
                         ([(('sale', exempt.pk), D('107'))], [], [('prepaid', D('50'))]))
        self.assertEqual(tax_ledger.owed_by_receipt([exempt, funded]), {})

    # 3. MEDIUM — a refund of a payment older than the data reopens that debt, so balance, ledger and agree.
    def test_refund_of_a_payment_before_the_data_reopens_its_debt(self):
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        self.refund('107')                                   # first thing in the data; QuickBooks' balance is 107
        ledger = self.ledger()
        self.assertEqual((ledger.balance(), self.balance(), [(lot.key, lot.amount, lot.tax) for lot in ledger.lots]), (D('107'), D('107'), [(None, D('107'), D('7.00'))]))
        exempt = self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', when=d(1))
        self.assertEqual(tax_ledger.owed_by_receipt([exempt]), {exempt.pk: D('100')})
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=d(2))        # pays the older debt first
        days = [self.both(d(n).date()) for n in range(3)]
        self.assertEqual([st.tax for st, _ in days], [D('-7.00'), D('0'), D('7.00')])
        self.assertEqual(self.account_rows(days[0][0]), [(DEFAULT_LOCATION, 'Payment on account · estimated', D('-107'), D('-7.00'), D('-100.00'), D('0.00'))])
        self.assertEqual(self.account_rows(days[2][0]), [(DEFAULT_LOCATION, 'Payment on account · estimated', D('107'), D('7.00'), D('100.00'), D('0.00'))])
        self.assertEqual((days[2][0].estimated_count, days[2][1].totals['exempt']), (1, D('0')))
        self.assertEqual((self.balance(), self.ledger().balance()), (D('100'), D('100')))
        self.assertEqual(tax_ledger.owed_by_receipt([exempt]), {exempt.pk: D('100')})
        self.assert_conserved()

    def test_refund_beyond_every_payment_in_the_data_reopens_the_rest_as_the_oldest_debt(self):
        d = lambda n: WHEN + timedelta(days=n)   # noqa: E731
        charged = self.doc(payments=(('account', '107'),), when=d(1))
        self.doc(total='107', tax='0', payments=(('cash', '107'),), poa=True, when=d(2))
        self.refund('150', when=d(3))                        # $107 undoes the payment, $43 paid back money older than the data
        self.assertEqual((self.balance(), self.ledger().balance()), (D('150'), D('150')))
        self.assertEqual([(lot.key, lot.amount, lot.tax) for lot in self.ledger().lots], [(None, D('43'), D('2.81')), (('sale', charged.pk), D('107'), D('7.00'))])
        self.assertEqual(tax_ledger.owed_by_receipt([charged]), {charged.pk: D('107')})
        self.doc(total='150', tax='0', payments=(('cash', '150'),), poa=True, when=d(4))
        days = [self.both(d(n).date()) for n in range(1, 5)]
        self.assertEqual([st.tax for st, _ in days], [D('0'), D('7.00'), D('-9.81'), D('9.81')])     # 43 × 7/107 = 2.81
        self.assertEqual(self.account_rows(days[3][0]), [(DEFAULT_LOCATION, 'Payment on account · estimated', D('150'), D('9.81'), D('140.19'), D('0.00'))])
        self.assertEqual((self.balance(), tax_ledger.owed_by_receipt([charged])), (D('0'), {}))
        self.assertEqual((list(self.ledger().lots), list(self.ledger().credits)), ([], []))


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class NativeAcceptReturnTests(ReturnTestCase):
    """Review 4, finding 1: returns made IN THE APP (Sales History → Accept Return, refund to Account) are saved as a
    negative receipt with a negative Account tender — not QuickBooks' Payment On Account shape — and must follow the
    same linked-return rule: merchandise still owed comes off its own charge with no tax row, only the rest is a
    credit carrying the tax that was collected. Everything here goes through `save_document`, the service the
    Accept Return view posts to, and checks the DAY each tax row lands on plus 's owed status."""

    def setUp(self):
        super().setUp()
        self.day0 = SALE_DAY - timedelta(days=1)              # 9/22
        self.day3 = RETURN_DAY + timedelta(days=1)            # 9/25
        self.day4 = RETURN_DAY + timedelta(days=2)            # 9/26

    def at(self, doc, when):
        Document.objects.filter(pk=doc.pk).update(completed_at=when)
        return Document.objects.get(pk=doc.pk)

    def both(self, start, end=None):
        p = period(start, end)
        st = sales_tax.build(p, detailed=True)
        dt = daily_totals.build(p)
        self.assertEqual(st.tax, dt.totals['tax'], f'Sales Tax {st.tax} vs Daily Totals {dt.totals["tax"]} for {p.label}')
        return st, dt

    def taxes(self, *days):
        return [self.both(d.date())[0].tax for d in days]

    def account_rows(self, st):
        # (location, label, total, tax, taxable, non-taxable). An Exempt receipt made in the app keeps its lines'
        # rates (the exemption is the receipt's flag), so the Exempt section lists its money under Taxable, tax 0.
        return [(s.location, r.type_label, r.total, r.tax, r.taxable, r.nontaxable) for s in st.sections for r in s.rows if r.kind == 'account']

    def ledger(self):
        return tax_ledger.replay(tax_ledger.customer_events(self.customer.pk)).ledgers.get(self.customer.pk)

    def owed(self, *docs):
        return tax_ledger.owed_by_receipt(list(Document.objects.filter(pk__in=[d.pk for d in docs]).select_related('customer').prefetch_related('tenders')))

    def history_status(self, doc):
        rows = self.client.get('/pos/history/', {'period': 'all', 'format': 'rows'}).json()['rows']
        return {r['id']: r for r in rows}[doc.pk]['cells'][2]

    def accept_return(self, sale, when=RETURN_DAY, **payment):
        payment = payment or dict(method='account', amount='-107.00')
        return self.at(self.save([self.returned(sale)], [payment]), when)

    def test_return_to_account_of_an_unpaid_charge_cancels_it_with_no_tax_row(self):
        # 1. $107 charged entirely to the account on day 1, returned to the account on day 2: no tax either day,
        # the receipt is not owed, the balance is zero and nothing is left on the ledger.
        sale = self.sell('1', payments=[dict(method='account', amount='107.00')])
        self.assertEqual((self.balance(), self.owed(sale), self.history_status(sale)), (D('107'), {sale.pk: D('107')}, 'On account · $107.00 owed'))
        ret = self.accept_return(sale)
        self.assertEqual((ret.total, ret.tax, ret.payment_breakdown), (D('-107.00'), D('-7.00'), [{'method': 'account', 'amount': '-107.00'}]))
        self.assertEqual(self.taxes(SALE_DAY, RETURN_DAY), [D('0'), D('0')])
        st, dt = self.both(RETURN_DAY.date())
        self.assertEqual((self.account_rows(st), st.reconciled_tax, dt.totals['received']), ([], D('0'), D('0')))
        row = [r for g in sales_tax.build_daily(one_day(RETURN_DAY.date())).groups for r in g.rows if r['number'] == ret.numbered_label][0]
        self.assertEqual((row['received'], row['account'], row['tax']), (D('0'), D('-107.00'), D('0')))
        self.assertEqual((self.balance(), self.owed(sale, ret), self.history_status(sale)), (D('0'), {}, 'Completed'))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), list(ledger.credits)), ([], []))
        whole, _ = self.both(SALE_DAY.date(), RETURN_DAY.date())
        self.assertEqual([(s.total, s.tax) for s in whole.sections], [(D('0'), D('0')), (D('0'), D('0'))])

    def test_return_comes_off_its_own_charge_not_an_older_exempt_one(self):
        # The reviewer's reproduction: an older Exempt charge, then the taxable one, returned to the account. The
        # old code reported −$7 on the return day and +$7 on the next payment, and left the wrong receipt owed.
        older = self.at(self.save([dict(item_id=self.chair.pk, quantity='1', price='107.00')], [dict(method='account', amount='107.00')], tax_exempt=True), self.day0)
        sale = self.sell('1', payments=[dict(method='account', amount='107.00')])
        self.assertEqual(self.owed(older, sale), {older.pk: D('107'), sale.pk: D('107')})
        ret = self.accept_return(sale)
        self.assertEqual((self.balance(), self.owed(older, sale, ret)), (D('107'), {older.pk: D('107')}))
        self.assertEqual((self.history_status(older), self.history_status(sale)), ('On account · $107.00 owed', 'Completed'))
        paid = self.at(self.save([dict(account_payment=True, price='107.00')], [dict(method='cash', amount='107.00')]), self.day3)
        self.assertEqual(self.taxes(self.day0, SALE_DAY, RETURN_DAY, self.day3), [D('0'), D('0'), D('0'), D('0')])
        self.assertEqual(self.account_rows(self.both(RETURN_DAY.date())[0]), [])
        st3, dt3 = self.both(self.day3.date())
        self.assertEqual(self.account_rows(st3), [(EXEMPT, 'Payment on account', D('107'), D('0'), D('107'), D('0'))])
        self.assertEqual((dt3.totals['received'], dt3.totals['exempt'], st3.estimated_count), (D('107'), D('107'), 0))
        self.assertEqual((self.balance(), self.owed(older, sale, ret, paid)), (D('0'), {}))
        self.assertEqual((list(self.ledger().lots), list(self.ledger().credits)), ([], []))

    def test_return_to_account_of_a_half_paid_charge_cancels_the_owed_half_and_carries_the_paid_tax(self):
        # 2. $107 sale: $53.50 cash + $53.50 on account (day 1: $3.50 tax on the cash). Returned to the account on
        # day 2: the owed half comes off the charge (its $3.50 was never collected — no row), the paid half is a
        # $53.50 credit carrying the $3.50 that WAS collected; nothing is reversed until that credit moves. On
        # day 3 it funds an Exempt charge, so the $3.50 is trued up: −$3.50 under Springfield, $53.50 Exempt.
        sale = self.sell('1', payments=[dict(method='cash', amount='53.50'), dict(method='account', amount='53.50')])
        self.assertEqual((self.balance(), self.owed(sale)), (D('53.50'), {sale.pk: D('53.50')}))
        ret = self.accept_return(sale)
        self.assertEqual((self.balance(), self.owed(sale, ret), self.history_status(sale)), (D('-53.50'), {}, 'Completed'))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), [(c.kind, c.amount, c.tax) for c in ledger.credits]), ([], [('return', D('53.50'), D('3.50'))]))
        exempt = self.at(self.save([dict(item_id=self.lamp.pk, quantity='1', price='53.50')], [dict(method='account', amount='53.50')], tax_exempt=True), self.day3)
        self.assertEqual(self.taxes(SALE_DAY, RETURN_DAY, self.day3), [D('3.50'), D('0'), D('-3.50')])
        st2, dt2 = self.both(RETURN_DAY.date())
        self.assertEqual((self.account_rows(st2), dt2.totals['received']), ([], D('0')))
        st3, dt3 = self.both(self.day3.date())
        self.assertEqual(self.account_rows(st3), [(DEFAULT_LOCATION, 'Return credit applied · tax trued up', D('-53.50'), D('-3.50'), D('-50.00'), D('0.00')),
                                                  (EXEMPT, 'Return credit applied · tax trued up', D('53.50'), D('0'), D('53.50'), D('0'))])
        self.assertEqual((dt3.totals['received'], dt3.totals['exempt']), (D('0'), D('53.50')))
        self.assertEqual((self.balance(), self.owed(sale, ret, exempt)), (D('0'), {}))
        whole, dt = self.both(SALE_DAY.date(), self.day3.date())
        self.assertEqual(([(s.total, s.tax) for s in whole.sections], dt.totals['received']), ([(D('0'), D('0.00')), (D('53.50'), D('0'))], D('53.50')))

    def test_partial_return_of_an_unpaid_charge_leaves_tax_only_on_the_kept_line(self):
        # 3. Chair $100 + lamp $50 = $160.50 incl. $10.50 tax, all on account (day 1). The chair comes back to the
        # account on day 2 ($107 off the charge, no row); the $53.50 still owed carries the lamp's $3.50, counted
        # on day 3 when it is paid.
        sale = self.at(self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00'), dict(item_id=self.lamp.pk, quantity='1', price='50.00')],
                                 [dict(method='account', amount='160.50')]), SALE_DAY)
        self.assertEqual((sale.total, sale.tax, self.owed(sale)), (D('160.50'), D('10.50'), {sale.pk: D('160.50')}))
        chair_line = sale.lines.get(item=self.chair)
        ret = self.at(self.save([self.returned(chair_line)], [dict(method='account', amount='-107.00')]), RETURN_DAY)
        self.assertEqual((ret.total, ret.tax), (D('-107.00'), D('-7.00')))
        self.assertEqual((self.balance(), self.owed(sale, ret), self.history_status(sale)), (D('53.50'), {sale.pk: D('53.50')}, 'On account · $53.50 owed'))
        ledger = self.ledger()
        self.assertEqual(([(lot.key, lot.amount, lot.tax, lot.taxable) for lot in ledger.lots], list(ledger.credits)),
                         ([(('sale', sale.pk), D('53.50'), D('3.50'), D('50.00'))], []))
        paid = self.at(self.save([dict(account_payment=True, price='53.50')], [dict(method='cash', amount='53.50')]), self.day3)
        self.assertEqual(self.taxes(SALE_DAY, RETURN_DAY, self.day3), [D('0'), D('0'), D('3.50')])
        self.assertEqual(self.account_rows(self.both(RETURN_DAY.date())[0]), [])
        st3, dt3 = self.both(self.day3.date())
        self.assertEqual(self.account_rows(st3), [(DEFAULT_LOCATION, 'Payment on account', D('53.50'), D('3.50'), D('50.00'), D('0.00'))])
        self.assertEqual((dt3.totals['received'], st3.estimated_count), (D('53.50'), 0))
        self.assertEqual((self.balance(), self.owed(sale, ret, paid), self.history_status(sale)), (D('0'), {}, 'Completed'))
        whole, _ = self.both(SALE_DAY.date(), self.day3.date())
        self.assertEqual((whole.sections[0].total, whole.sections[0].tax), (D('53.50'), D('3.50')))

    def test_return_to_account_of_a_paid_receipt_is_a_credit_carrying_its_tax(self):
        # 4. Paid in cash on day 1 ($7 tax), returned to the account on day 2: a $107 credit carrying the $7, no row
        # (the cash is still in the drawer). Spent on an Exempt charge on day 3: trued up, −$7 Springfield.
        sale = self.sell('1')
        ret = self.accept_return(sale)
        self.assertEqual((self.balance(), self.owed(sale, ret), self.history_status(sale)), (D('-107'), {}, 'Completed'))
        ledger = self.ledger()
        self.assertEqual((list(ledger.lots), [(c.kind, c.amount, c.tax, c.taxable) for c in ledger.credits]), ([], [('return', D('107'), D('7'), D('100'))]))
        exempt = self.at(self.save([dict(item_id=self.chair.pk, quantity='1', price='107.00')], [dict(method='account', amount='107.00')], tax_exempt=True), self.day3)
        self.assertEqual(self.taxes(SALE_DAY, RETURN_DAY, self.day3), [D('7'), D('0'), D('-7')])
        self.assertEqual(self.account_rows(self.both(RETURN_DAY.date())[0]), [])
        st3, dt3 = self.both(self.day3.date())
        self.assertEqual(self.account_rows(st3), [(DEFAULT_LOCATION, 'Return credit applied · tax trued up', D('-107'), D('-7'), D('-100'), D('0.00')),
                                                  (EXEMPT, 'Return credit applied · tax trued up', D('107'), D('0'), D('107'), D('0'))])
        self.assertEqual((dt3.totals['received'], dt3.totals['exempt'], self.balance(), self.owed(sale, ret, exempt)), (D('0'), D('107'), D('0'), {}))
        whole, dt = self.both(SALE_DAY.date(), self.day3.date())
        self.assertEqual(([(s.total, s.tax) for s in whole.sections], dt.totals['received']), ([(D('0'), D('0')), (D('107'), D('0'))], D('107')))
