# SPDX-License-Identifier: AGPL-3.0-or-later
"""sales orders imported from QuickBooks POS that are still OPEN work like orders
made in this app — deposits/payments on top of QB's deposit, pickups, cancel with the unused-deposit refund, and
line/price edits — while QB receipts and closed/cancelled QB orders stay read-only history. "We have
lost the ability to take a deposit (payment) on a sales order" (every open order on the live server is a QB import).
"""
import uuid
from datetime import datetime, timedelta, date
from decimal import Decimal as D
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from customers.models import Customer
from sales import accounts
from sales.models import Document, Item, Line, OrderPayment, Tender, DiscountReason
from sales.services import save_document, save_order_payment, fulfill_order, WorkflowError

NY = ZoneInfo('America/New_York')
TODAY = datetime(2026, 10, 19, 12, tzinfo=NY)
QB_DAY = TODAY - timedelta(days=5)


def period(start, end=None):
    from reports import ny_periods
    return ny_periods.resolve('custom', '', start, start, end or start)


class ImportedOrderBase(TestCase):
    """One QB order: 2 x $100 chair + 7 % = 214.00; QuickBooks took a $50 deposit (receipt 900, type 2)."""

    def setUp(self):
        self.customer = Customer.objects.create(source_id='qb-cust', full_name='TEST QB CUSTOMER', charge_account=True)
        self.item = Item.objects.create(sku='CHAIR', name='Chair', description='Oak chair', price=D('100'), on_hand=5, tax_rate=D('7'))
        self.order = Document.objects.create(kind='order', status='open', source_id='QBSO-1', source_number=5400, customer=self.customer,
                                             customer_name='TEST QB CUSTOMER', subtotal=D('200'), tax=D('14'), total=D('214'),
                                             deposit_taken=D('50'), deposit_used=D('0'), tax_location='Springfield')
        Document.objects.filter(pk=self.order.pk).update(created_at=QB_DAY - timedelta(days=1))
        self.line = Line.objects.create(document=self.order, position=0, item=self.item, sku='CHAIR', item_name='Chair', description='Oak chair',
                                        quantity=2, price=D('100'), original_price=D('100'), tax_rate=D('7'), source_id='QBSO-1:0')
        self.deposit = Document.objects.create(kind='sale', status='completed', source_id='QBR-900', source_number=900, receipt_type=2,
                                               deposit_order=self.order, customer=self.customer, customer_name='TEST QB CUSTOMER',
                                               subtotal=D('0'), tax=D('0'), total=D('50'), completed_at=QB_DAY, tax_location='Springfield')
        Tender.objects.create(document=self.deposit, position=1, method='cash', amount=D('50'))
        self.order.refresh_from_db()

    def pay(self, amount, method='cash', when=TODAY, **extra):
        """Record a payment as if the clock read `when` (a payment is dated the day it is recorded, never
        by a posted date, so the test pins the clock)."""
        self.order.refresh_from_db()
        data = dict(version=self.order.version, token=str(uuid.uuid4()), amount=str(amount), method=method, **extra)
        with mock.patch('django.utils.timezone.now', return_value=when):
            save_order_payment(self.order.pk, data)
        self.order.refresh_from_db()
        return self.order.payments.filter(adjustment=False).order_by('pk').last()

    def pickup(self, qty, when=TODAY):
        self.order.refresh_from_db()
        with mock.patch('django.utils.timezone.now', return_value=when):
            receipt = fulfill_order(self.order.pk, {'version': self.order.version, 'token': str(uuid.uuid4()), f'qty_{self.line.pk}': str(qty)})
        self.order.refresh_from_db()
        return receipt

    def register_rows(self):
        from sales.views import document_data
        return document_data(self.order)['lines']

    def save(self, rows=None, **extra):
        self.order.refresh_from_db()
        data = dict(action='save', version=self.order.version, customer_id=self.customer.pk, token=str(self.order.token),
                    discount_percent=str(self.order.discount_percent), discount_amount='0', lines=rows or self.register_rows())
        data.update(extra)
        return save_document(data, 'order', self.order.token)


class ReadOnlyScope(ImportedOrderBase):
    def test_open_qb_order_is_workable_closed_qb_order_and_qb_receipt_stay_read_only(self):
        self.assertTrue(self.order.from_quickbooks)
        self.assertFalse(self.order.imported)
        self.assertTrue(self.deposit.imported)
        for status in ('completed', 'cancelled'):
            closed = Document.objects.create(kind='order', status=status, source_id=f'QBSO-{status}', source_number=1, customer=self.customer, total=D('10'))
            self.assertTrue(closed.imported, status)
            with self.assertRaisesMessage(WorkflowError, 'read-only'):
                save_order_payment(closed.pk, dict(version=closed.version, token=str(uuid.uuid4()), amount='5', method='cash'))
            with self.assertRaisesMessage(WorkflowError, 'read-only'):
                fulfill_order(closed.pk, dict(version=closed.version, token=str(uuid.uuid4())))
            with self.assertRaisesMessage(WorkflowError, 'read-only'):
                save_document(dict(action='cancel', version=closed.version), 'order', closed.token)
        with self.assertRaisesMessage(WorkflowError, 'read-only'):
            save_document(dict(action='cancel', version=self.deposit.version), 'sale', self.deposit.token)

    def test_order_page_shows_the_payment_form_with_qb_deposit_counted_once(self):
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertFalse(page.context['readonly'])
        self.assertContains(page, 'Payments and Pickup')
        self.assertContains(page, 'Deposit taken in QuickBooks POS $50.00')
        self.assertContains(page, 'Payments $50.00 · Balance due <span id="order-balance-due" data-amount="164.00">$164.00</span>')
        self.assertNotContains(page, 'were recorded in QuickBooks POS as deposit receipts')
        # No 25 % prefill when QuickBooks already took a deposit; the form's version/token are the order's.
        self.assertContains(page, 'name="amount" type="number" step="0.01" required value=""')
        self.assertContains(page, 'Take Payment for Charge Account')
        self.assertFalse(page.context['initial']['imported'])
        # The closed QB order keeps the read-only summary.
        self.order.status = 'completed'; self.order.save()
        page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertTrue(page.context['readonly'])
        self.assertContains(page, 'Deposits and Payments')
        self.assertContains(page, 'Deposit taken in QuickBooks POS $50.00')
        self.assertNotContains(page, 'Payments and Pickup')


class PaymentsAndPickups(ImportedOrderBase):
    def test_cash_payment_adds_to_qb_deposit(self):
        payment = self.pay('100')
        self.assertEqual((payment.amount, payment.method), (D('100.00'), 'cash'))
        self.assertEqual((self.order.paid, self.order.balance_due, self.order.unused_payments, self.order.status), (D('150.00'), D('64.00'), D('150.00'), 'open'))
        with mock.patch('django.utils.timezone.now', return_value=TODAY):   # the payment's own day: fully editable
            page = self.client.get(f'/pos/order/{self.order.token}/')
        self.assertContains(page, 'Payments $150.00 · Balance due <span id="order-balance-due" data-amount="64.00">$64.00</span>')
        self.assertContains(page, 'Save Correction')
        self.assertContains(page, 'Delete Payment')
        self.assertNotContains(page, 'name="payment_date"')          # a payment is dated the day it is recorded
        self.assertContains(page, 'Date: today')
        page = self.client.get(f'/pos/order/{self.order.token}/')   # the next day: money fields fixed, reference editable
        self.assertNotContains(page, 'Save Correction')
        self.assertNotContains(page, 'Delete Payment')
        self.assertContains(page, 'Save Reference')
        self.assertContains(page, 'This payment is from an earlier day. To change it, record a refund or a new payment today.')

    def test_payment_by_account_charges_the_imported_customer(self):
        self.pay('64', method='account')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.account_balance, D('64.00'))
        self.assertEqual(accounts.order_charged(self.order), D('64.00'))
        self.assertEqual(self.order.balance_due, D('100.00'))
        self.order.payments.all().delete(); accounts.refresh(self.customer.pk)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.account_balance, D('0.00'))

    def test_partial_then_final_pickup_deducts_stock_squares_tax_and_completes(self):
        # A pickup never runs ahead of the money: QB's 50 + 57 more covers the first chair (107).
        with self.assertRaisesMessage(WorkflowError, 'Record another $57.00'):
            self.pickup(1)
        self.pay('57')
        receipt = self.pickup(1)
        self.item.refresh_from_db(); self.line.refresh_from_db()
        self.assertEqual((receipt.subtotal, receipt.tax, receipt.total, receipt.source_order_id), (D('100.00'), D('7.00'), D('107.00'), self.order.pk))
        self.assertEqual((self.item.on_hand, self.line.fulfilled, self.order.unfilled, self.order.status), (4, 1, 1, 'open'))
        self.assertEqual((self.order.paid, self.order.applied_payments, self.order.unused_payments), (D('107.00'), D('107.00'), D('0.00')))
        with self.assertRaisesMessage(WorkflowError, 'Record another $107.00'):
            self.pickup(1)
        self.pay('107', method='credit')
        final = self.pickup(1)
        self.item.refresh_from_db()
        self.assertEqual((final.tax, final.total, self.item.on_hand), (D('7.00'), D('107.00'), 3))
        self.assertEqual((self.order.status, self.order.balance_due, self.order.unused_payments, self.order.unfilled), ('completed', D('0.00'), D('0.00'), 0))
        self.assertTrue(self.order.imported)  # closed now: read-only QB history again
        history = self.client.get('/pos/history/?status=all')
        self.assertContains(history, f'/pos/sale/{final.token}/')
        # The pickup receipt page and printout name both the QB deposit and the app payment.
        page = self.client.get(f'/pos/sale/{final.token}/')
        self.assertContains(page, 'Deposit taken in QuickBooks POS $50.00')
        self.assertContains(page, 'Credit card $107.00')
        from sales.printing import Receipt, render_receipt
        drawn = []
        original = Receipt.text
        def spy(self_, text, *args, **kwargs):
            drawn.append(text); return original(self_, text, *args, **kwargs)
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(final)
        self.assertIn('Deposit taken in QuickBooks POS:', drawn)
        self.assertIn('Credit card:', drawn)

    def test_letter_sales_order_lists_qb_deposit_then_app_payments(self):
        from sales.letter_printing import Pages, render_sales_order, deposit_rows
        self.assertEqual(deposit_rows(self.order), [(f'{QB_DAY:%-m/%-d/%Y}', 'Deposit taken in QB POS', D('50.00'), 'QuickBooks')])
        self.pay('100', method='check', check_number='123', reference='Mailed in')
        rows = deposit_rows(self.order)
        self.assertEqual([(r[1], r[2], r[3]) for r in rows], [('Deposit taken in QB POS', D('50.00'), 'QuickBooks'), ('Mailed in', D('100.00'), 'Check #123')])
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(self.order)
        texts = [step[3] for steps in pages for step in steps if step[0] == 'text']
        self.assertIn('Deposit History', texts)
        self.assertIn('Deposit taken in QB POS', texts)
        self.assertIn('QuickBooks', texts)
        self.assertIn('Check #123', texts)
        self.assertIn('Mailed in', texts)
        # Deposit Balance = QB 50 + app 100; Balance Due = 214 - 150.
        self.assertEqual(texts[texts.index('Deposit Balance:') + 1], '$150.00')
        self.assertEqual(texts[texts.index('Balance Due:') + 1], '$64.00')
        # Several QB deposit receipts: one row, dated by the latest, count in the reference.
        Document.objects.create(kind='sale', status='completed', source_id='QBR-901', source_number=901, receipt_type=2, deposit_order=self.order,
                                customer=self.customer, total=D('25'), tax=D('0'), completed_at=QB_DAY + timedelta(days=2))
        self.order.deposit_taken = D('75'); self.order.save()
        self.assertEqual(deposit_rows(self.order)[0], (f'{QB_DAY + timedelta(days=2):%-m/%-d/%Y}', 'Deposit taken in QB POS (2)', D('75.00'), 'QuickBooks'))
        # No linked receipt: the order's own date.
        self.order.deposit_receipts.all().delete()
        self.assertEqual(deposit_rows(self.order)[0][0], f'{QB_DAY - timedelta(days=1):%-m/%-d/%Y}')


class Cancel(ImportedOrderBase):
    def test_cancel_needs_the_refund_choice_for_qb_deposit_and_records_it(self):
        with self.assertRaisesMessage(WorkflowError, 'holds $50.00 in unused deposits'):
            save_document(dict(action='cancel', version=self.order.version), 'order', self.order.token)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'open')
        self.pay('30')
        for method in ('cash', 'credit', 'account'):
            with self.subTest(method=method):
                with self.assertRaisesMessage(WorkflowError, 'changed'):
                    save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method=method, deposit_refund_amount='50'), 'order', self.order.token)
                done = save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method=method, deposit_refund_amount='80'), 'order', self.order.token)
                refund = done.payments.order_by('pk').last()
                self.assertEqual((done.status, refund.amount, refund.method, refund.reference), ('cancelled', D('-80.00'), method, 'Deposit refund on cancellation'))
                self.assertEqual((done.paid, done.unused_payments), (D('0.00'), D('0.00')))
                self.assertTrue(done.imported)
                self.customer.refresh_from_db()
                self.assertEqual(self.customer.account_balance, D('-80.00') if method == 'account' else D('0.00'))
                refund.delete(); Document.objects.filter(pk=done.pk).update(status='open'); accounts.refresh(self.customer.pk); self.order.refresh_from_db()

    def test_register_json_carries_the_unused_deposit_for_the_refund_dialog(self):
        from sales.views import document_data
        self.assertEqual(document_data(self.order)['unused_deposit'], '50.00')


class Edits(ImportedOrderBase):
    def test_resave_without_a_money_change_keeps_qb_recorded_totals(self):
        # QB recorded 12.00 shipping the lines don't carry (4 such open orders in the 10/18 data).
        Document.objects.filter(pk=self.order.pk).update(tax=D('14.84'), total=D('226.84'))
        saved = self.save(notes='Deliver Friday')
        self.assertEqual((saved.subtotal, saved.discount, saved.tax, saved.total, saved.notes, saved.status), (D('200.00'), D('0.00'), D('14.84'), D('226.84'), 'Deliver Friday', 'open'))
        self.line.refresh_from_db()
        self.assertEqual(self.line.source_id, 'QBSO-1:0')   # still QuickBooks' line
        self.assertEqual(saved.balance_due, D('176.84'))

    def test_quantity_change_recomputes_like_an_app_order_and_releases_the_line(self):
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        saved = self.save(rows)
        self.assertEqual((saved.subtotal, saved.tax, saved.total, saved.balance_due), (D('300.00'), D('21.00'), D('321.00'), D('271.00')))
        self.assertGreater(saved.version, self.order.version)
        self.line.refresh_from_db()
        self.assertIsNone(self.line.source_id)
        self.assertEqual(saved.source_id, 'QBSO-1')   # the order itself keeps its QB identity and number
        self.assertEqual(saved.number, '5400')
        stale = dict(action='save', version=1, customer_id=self.customer.pk, lines=rows, discount_percent='0', discount_amount='0')
        with self.assertRaisesMessage(WorkflowError, 'Someone changed this document'):
            save_document(stale, 'order', self.order.token)

    def test_added_line_and_price_edit(self):
        rows = self.register_rows() + [dict(item_id=None, sku='', item_name='SPECIAL BENCH', description='Special bench', vendor='', quantity='1', price='55.00', tax_rate='7')]
        rows[0]['price'] = '90.00'; rows[0]['original_price'] = '100.00'
        saved = self.save(rows)
        self.assertEqual([(l.description, l.quantity, l.price, l.source_id) for l in saved.lines.all()],
                         [('Oak chair', D('2.000'), D('90.0000'), None), ('Special bench', D('1.000'), D('55.0000'), None)])
        self.assertEqual((saved.subtotal, saved.tax, saved.total), (D('235.00'), D('16.45'), D('251.45')))

    def test_qb_order_discount_needs_no_reason_until_it_changes(self):
        DiscountReason.objects.get_or_create(name='Sale')
        Document.objects.filter(pk=self.order.pk).update(discount_percent=D('5'), discount=D('10'), tax=D('13.30'), total=D('203.30'))
        Line.objects.filter(pk=self.line.pk).update(receipt_discount=D('10'))
        saved = self.save(notes='same discount')
        self.assertEqual((saved.discount, saved.discount_percent, saved.tax, saved.total, saved.discount_reason), (D('10.00'), D('5.00'), D('13.30'), D('203.30'), ''))
        with self.assertRaisesMessage(WorkflowError, 'Choose a reason for the order discount'):
            self.save(discount_percent='10')
        saved = self.save(discount_percent='10', discount_reason='Sale')
        self.assertEqual((saved.discount, saved.discount_percent, saved.tax, saved.total), (D('20.00'), D('10.00'), D('12.60'), D('192.60')))
        self.line.refresh_from_db()
        self.assertIsNone(self.line.source_id)


    def test_qb_typed_dollar_discount_keeps_its_amount_while_the_percent_is_left_alone(self):
        # QB recorded $11.00 off 200.00 and stored the percent as 5.50 %; 5.5 % of 200 is exactly 11.00, so make the
        # echo inexact the way a shop's orders are: $11.00 with percent 5.49 (5.49 % of 200 = 10.98).
        DiscountReason.objects.get_or_create(name='Sale')
        Document.objects.filter(pk=self.order.pk).update(discount_percent=D('5.49'), discount=D('11'), tax=D('13.23'), total=D('202.23'))
        Line.objects.filter(pk=self.line.pk).update(receipt_discount=D('11'))
        saved = self.save(notes='no change')
        self.assertEqual((saved.discount, saved.discount_percent, saved.tax, saved.total), (D('11.00'), D('5.49'), D('13.23'), D('202.23')))
        self.line.refresh_from_db()
        self.assertEqual((self.line.receipt_discount, self.line.source_id), (D('11.00'), 'QBSO-1:0'))
        # A quantity edit with the percent untouched keeps QB's $11.00 (3 x 100 - 11 = 289 x 7 % = 20.23).
        rows = self.register_rows(); rows[0]['quantity'] = '3'
        saved = self.save(rows)
        self.assertEqual((saved.discount, saved.discount_percent, saved.subtotal, saved.tax, saved.total), (D('11.00'), D('5.49'), D('300.00'), D('20.23'), D('309.23')))
        # Changing the percent re-applies it like an app order (needs a reason): 6 % of 300 = 18.00.
        saved = self.save(discount_percent='6', discount_reason='Sale')
        self.assertEqual((saved.discount, saved.discount_percent, saved.tax, saved.total), (D('18.00'), D('6.00'), D('19.74'), D('301.74')))


class TaxLedger(ImportedOrderBase):
    """Tax counts on money received: the QB deposit's share on its day, an app payment's share on its day,
    cumulative per order so the two never double-count and a full payment squares to the order's tax."""

    def test_app_payment_on_qb_order_counts_only_its_own_share_and_ties_to_daily_totals(self):
        from reports import sales_tax, daily_totals
        self.pay('100', when=TODAY)
        self.pay('64', method='credit', when=TODAY + timedelta(days=1))
        qb_day, day1, day2 = QB_DAY.date(), TODAY.date(), (TODAY + timedelta(days=1)).date()
        # 50/214 × 14 = 3.27 on the QB day; 150/214 × 14 = 9.81 − 3.27 = 6.54 today; the rest 4.19 tomorrow.
        expected = {qb_day: D('3.27'), day1: D('6.54'), day2: D('4.19')}
        for day, tax in expected.items():
            with self.subTest(day=day):
                report = sales_tax.build(period(day), detailed=True)
                self.assertEqual(report.tax, tax)
                self.assertEqual(daily_totals.build(period(day)).totals['tax'], tax)
        whole = sales_tax.build(period(qb_day, day2), detailed=True)
        self.assertEqual(whole.tax, D('14.00'))
        self.assertEqual(daily_totals.build(period(qb_day, day2)).totals['tax'], D('14.00'))
        rows = [r for s in whole.sections for r in s.rows if 'Sales Order 5400' in r.type_label]
        self.assertEqual(sorted((r.type_label.split(' · ')[0], r.tax) for r in rows),
                         [('Order deposit', D('3.27')), ('Order payment', D('4.19')), ('Order payment', D('6.54'))])

    def test_cancel_refund_reverses_the_tax_counted_on_the_qb_deposit(self):
        from django.utils import timezone
        from reports import sales_tax, daily_totals
        save_document(dict(action='cancel', version=self.order.version, refund_deposit=True, deposit_refund_method='cash', deposit_refund_amount='50'), 'order', self.order.token)
        real_today = timezone.localdate()   # the refund OrderPayment is stamped with the real clock
        today = sales_tax.build(period(real_today), detailed=True)
        self.assertEqual(today.tax, D('-3.27'))
        self.assertEqual(daily_totals.build(period(real_today)).totals['tax'], D('-3.27'))
        self.assertEqual(sales_tax.build(period(QB_DAY.date(), real_today)).tax, D('0.00'))

    def test_account_payment_on_qb_order_is_deferred_until_paid(self):
        from reports import sales_tax, daily_totals
        self.pay('164', method='account', when=TODAY)
        today = sales_tax.build(period(TODAY.date()), detailed=True)
        self.assertEqual((today.tax, today.charged_to_account, today.charged_to_account_tax), (D('0.00'), D('164.00'), D('10.73')))
        self.assertEqual(daily_totals.build(period(TODAY.date())).totals['tax'], D('0.00'))
        # Paying the account later releases the deferred share (QB deposit 3.27 + this 10.73 = the order's 14.00).
        with mock.patch('django.utils.timezone.now', return_value=TODAY + timedelta(days=2)):
            save_document(dict(action='complete', version=0, customer_id=self.customer.pk,
                               lines=[dict(account_payment=True, price='164.00')], payments=[dict(method='cash', amount='164.00')]), 'sale', uuid.uuid4())
        later = (TODAY + timedelta(days=2)).date()
        self.assertEqual(sales_tax.build(period(later), detailed=True).tax, D('10.73'))
        self.assertEqual(daily_totals.build(period(later)).totals['tax'], D('10.73'))
        self.assertEqual(sales_tax.build(period(QB_DAY.date(), later)).tax, D('14.00'))
