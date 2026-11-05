# SPDX-License-Identifier: AGPL-3.0-or-later
"""order payment tax figures are locked on the day the money is received. The backfill
(`tax_ledger.lock_all_orders`, `manage.py lock_order_payment_tax`) stores exactly what the ledger reports today, so
Sales Tax and Daily Totals for every past day are identical before and after it, and it never overwrites a value."""
import random
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from io import StringIO
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.db.models import Sum

from customers.models import Customer
from sales.models import Document, Line, OrderPayment, Tender, money
from reports import daily_totals, sales_tax, tax_ledger
from reports.test_tax_ledger import LedgerFixture, period

NY = ZoneInfo('America/New_York')
START, END = date(2026, 8, 20), date(2026, 9, 30)


def snapshot(start, end, money_only=False):
    """Every figure both reports show for [start, end], day by day and as a whole, as plain tuples. `money_only`
    leaves out Daily Totals' New Sale / Balance columns, which by design show an order's CURRENT total on its
    creation day (they follow an edit; the tax and money-received columns must not)."""
    skip = {'new_sale', 'balance'} if money_only else set()
    out = []
    for a, b in [(start, end)] + [(start + timedelta(days=i),) * 2 for i in range((end - start).days + 1)]:
        p = period(a, b)
        st = sales_tax.build(p, detailed=True)
        dt = daily_totals.build(p)
        assert st.tax == dt.totals['tax'], (a, b, st.tax, dt.totals['tax'])
        for section in st.sections:
            out.append((a, b, section.location, section.receipts, section.gross, section.discount, section.nontaxable, section.taxable, section.tax, section.total))
            out += [(a, b, section.location) + tuple(getattr(r, k) for k in ('date', 'receipt', 'customer', 'type_label', 'gross', 'discount', 'nontaxable', 'taxable', 'tax', 'total', 'kind'))
                    for r in section.rows]
        out.append((a, b, 'summary', st.charged_to_account, st.charged_to_account_tax, st.payments_on_account, st.payments_on_account_tax,
                    st.estimated_tax, st.reconciled_tax, st.unlinked_deposits, st.other_locations))
        out += [(a, b, 'daily') + tuple((k, row[k]) for k in sorted(row) if k not in skip) for row in [r for g in dt.groups for r in g.rows]]
        out.append((a, b, 'daily-total') + tuple((k, v) for k, v in sorted(dt.totals.items()) if k not in skip))
    return out


class OrderTaxLockFixture(LedgerFixture):
    """A random mix like a shop's: QB orders with deposit receipts (cash, account, exempt customers, refunds,
    one with the deposit already used on a QB pickup), app orders with app payments (every method, one beyond its
    total then refunded), orders with taxed and untaxed lines, and payments on account that release deferred tax."""

    def setUp(self):
        super().setUp()
        rng = random.Random(4820)
        customers = [self.customer] + [Customer.objects.create(source_id=f'r{i}', first_name=f'R{i}', charge_account=True,
                                                               tax_location='Exempt' if i == 3 else '', tax_exempt=i == 3) for i in range(4)]
        start = datetime(2026, 8, 20, 10, tzinfo=NY)
        orders = []
        for i in range(140):
            when = start + timedelta(hours=rng.randint(0, 24 * 40))
            customer = rng.choice(customers)
            exempt = customer.tax_exempt
            roll = rng.random()
            if roll < 0.12:
                self.doc(total=str(rng.randint(10, 500)), tax='0', payments=(('check', None),), poa=True, when=when, customer=customer)
                doc = Document.objects.latest('pk')
                doc.payment_breakdown = [{'method': 'check', 'amount': str(doc.total)}]
                doc.save()
            elif roll < 0.30:
                taxed, untaxed = D(rng.randint(100, 3000)), D(rng.choice([0, 0, 50, 125]))
                tax = money(taxed * D('0.07')) if not exempt else D('0')
                qb = rng.random() < 0.6
                order = self.doc(order=True, total=str(taxed + untaxed + tax), tax=str(tax), when=when, customer=customer, payments=(),
                                 tax_location='Exempt' if exempt else '',
                                 **(dict(imported=True, deposit_taken=D('0'), deposit_used=D('0')) if qb else {}))
                Line.objects.create(document=order, position=1, item_name='TAXED', description='Taxed', quantity=1, price=taxed, tax_rate=7)
                if untaxed:
                    Line.objects.create(document=order, position=2, item_name='FREE', description='Untaxed', quantity=1, price=untaxed, tax_rate=0)
                orders.append(order)
            elif roll < 0.55 and orders:
                order = rng.choice(orders)
                amount = money(order.total * D(rng.choice(['0.25', '0.3333', '0.5', '1', '1.1'])))
                method = rng.choice(['cash', 'credit', 'account', 'check'])
                if rng.random() < 0.15:
                    amount = -money(amount / 3)   # a refund
                if order.from_quickbooks:
                    # A QuickBooks deposit (type 2) or deposit refund (type 3) receipt linked to the order.
                    self.counter += 1
                    receipt = Document.objects.create(kind='sale', status='completed', receipt_type=2 if amount > 0 else 3, deposit_order=order,
                                                      customer=customer, customer_name=customer.display_name, subtotal=D('0'), tax=D('0'),
                                                      total=amount, completed_at=when, source_id=f'dep-{self.counter}',
                                                      tax_location='Exempt' if exempt else 'Springfield', tax_exempt=True)
                    Tender.objects.create(document=receipt, position=0, method=method, amount=amount)
                    Document.objects.filter(pk=order.pk).update(deposit_taken=(order.deposit_taken or D('0')) + amount)
                    order.refresh_from_db()
                else:
                    OrderPayment.objects.create(order=order, amount=amount, method=method, created_at=when)
            else:
                pre = D(rng.randint(5, 2000))
                tax = money(pre * D('0.07')) if not exempt else D('0')
                total = pre + tax
                split = rng.random()
                if split < 0.3:
                    account = money(total * D(rng.choice(['0.25', '0.5', '1'])))
                    payments = (('credit', str(total - account)), ('account', str(account)))
                else:
                    payments = ((rng.choice(['cash', 'check', 'credit', 'debit']), str(total)),)
                self.doc(total=str(total), tax=str(tax), payments=payments, when=when, customer=customer,
                         imported=rng.random() < 0.5, tax_location='Exempt' if exempt else 'Springfield')
        # One QB order whose deposit QuickBooks already used on a pickup there: its deposit receipts are not counted.
        used = next(o for o in orders if o.from_quickbooks and o.deposit_receipts.exists())
        Document.objects.filter(pk=used.pk).update(deposit_used=D('10'))
        self.orders = orders

    def counted(self):
        deposits = Document.objects.filter(kind='sale', status='completed', receipt_type__in=(2, 3), deposit_order__isnull=False).exclude(deposit_order__deposit_used__gt=0)
        return list(deposits) + list(OrderPayment.objects.all())


class BackfillParityTests(OrderTaxLockFixture):
    def test_reports_are_identical_before_and_after_the_backfill_and_every_payment_is_locked(self):
        rows = self.counted()
        self.assertGreater(len(rows), 30)
        self.assertTrue(all(r.tax_lock is None for r in rows))
        before = snapshot(START, END)
        counts = tax_ledger.lock_all_orders()
        self.assertEqual(counts['payments_locked'], len(rows))
        self.assertEqual(counts['payments_already_locked'], 0)
        self.assertEqual(before, snapshot(START, END))
        for row in self.counted():
            lock = row.tax_lock
            self.assertIsNotNone(lock, row)
            amount = money(row.total if isinstance(row, Document) else row.amount)
            self.assertEqual(lock.tax + lock.taxable + lock.nontaxable, amount, row)
            self.assertIn(lock.location, ('Springfield', 'Exempt'))
        self.assertTrue(any(r.tax_lock.account_taxable for r in self.counted()))   # Account-paid deposits keep their split
        self.assertTrue(any(r.tax_lock.location == 'Exempt' for r in self.counted()))

    def test_backfill_is_idempotent_and_never_overwrites(self):
        tax_ledger.lock_all_orders()
        stored = {(type(r).__name__, r.pk): r.tax_lock for r in self.counted()}
        counts = tax_ledger.lock_all_orders()
        self.assertEqual((counts['payments_locked'], counts['payments_already_locked']), (0, len(stored)))
        # The orders change (as edits will): a second run still stores nothing new and the reports keep their figures.
        before = snapshot(START, END, money_only=True)
        for order in self.orders:
            Document.objects.filter(pk=order.pk).update(tax=D('0'), tax_exempt=True, total=order.total - order.tax)
        counts = tax_ledger.lock_all_orders()
        self.assertEqual(counts['payments_locked'], 0)
        self.assertEqual({(type(r).__name__, r.pk): r.tax_lock for r in self.counted()}, stored)
        self.assertEqual(before, snapshot(START, END, money_only=True))

    def test_management_command_dry_runs_by_default_and_commits_on_request(self):
        out = StringIO()
        call_command('lock_order_payment_tax', stdout=out, stderr=StringIO())
        self.assertIn('"committed": false', out.getvalue())
        self.assertTrue(all(r.tax_lock is None for r in self.counted()))
        out = StringIO()
        call_command('lock_order_payment_tax', '--commit', stdout=out, stderr=StringIO())
        self.assertIn('"committed": true', out.getvalue())
        self.assertIn(f'"payments_locked": {len(self.counted())}', out.getvalue())
        self.assertTrue(all(r.tax_lock is not None for r in self.counted()))

    def test_an_edit_after_the_backfill_moves_only_the_next_payments_tax(self):
        tax_ledger.lock_all_orders()
        order = next(o for o in self.orders if not o.from_quickbooks and o.tax and o.payments.filter(amount__gt=0).exists()
                     and o.payments.aggregate(s=Sum('amount'))['s'] < o.total)
        before = snapshot(START, END, money_only=True)
        booked = sum(l.tax for l in tax_ledger.order_tax_locks(order).values())
        Document.objects.filter(pk=order.pk).update(total=order.total + D('107'), tax=order.tax + D('7'))   # a $100 chair added
        order.refresh_from_db()
        self.assertEqual(before, snapshot(START, END, money_only=True))                 # nothing already reported moved
        payment = OrderPayment.objects.create(order=order, amount=order.total - order.paid, method='cash', created_at=datetime(2026, 10, 1, 12, tzinfo=NY))
        tax_ledger.lock_payment(order, payment)
        payment.refresh_from_db()
        self.assertEqual(payment.locked_tax, order.tax - booked)       # the remainder, chair's tax included
        self.assertEqual(sum(l.tax for l in tax_ledger.order_tax_locks(order).values()), order.tax)
        self.assertEqual(before, snapshot(START, END, money_only=True))
        st, dt = self.both(date(2026, 10, 1))
        self.assertEqual(st.tax, order.tax - booked)
