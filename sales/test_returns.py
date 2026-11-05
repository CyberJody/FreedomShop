# SPDX-License-Identifier: AGPL-3.0-or-later
"""Returns, refunds and exchanges.

A return is negative-quantity lines on an ordinary sales receipt, each linked to the receipt line it returns, at that
line's price and tax rate; new items on the same receipt make an exchange and the receipt nets. The cashier chooses the
refund tender (cash, card or Account); the returned quantity is capped at the quantity sold on that receipt (QB).
"""
import uuid
from datetime import datetime
from decimal import Decimal as D
from unittest import mock
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.test import TestCase

from customers.models import Customer
from reports import general_sales, ny_periods, sales_tax

from .models import DiscountReason, Document, Item, Line, StockMovement
from .printing import Receipt, render_receipt
from .services import WorkflowError, save_document

NY = ZoneInfo('America/New_York')
SALE_DAY = datetime(2026, 9, 23, 11, tzinfo=NY)
RETURN_DAY = datetime(2026, 9, 24, 14, tzinfo=NY)


def day(d):
    return ny_periods.resolve('custom', '', d, d, d)


class ReturnTestCase(TestCase):
    def setUp(self):
        # These tests date receipts in September and then edit/cancel them: pin the store day to the sale day so the
        # receipt lock (test_receipt_lock.py) doesn't apply here.
        pin = mock.patch('sales.models.store_today', return_value=SALE_DAY.date())
        pin.start()
        self.addCleanup(pin.stop)
        self.chair = Item.objects.create(sku='6045', name='TEST CHAIR', description='TEST CHAIR', price='100.00', on_hand='5',
                                         tax_rate='7.000', average_cost='40.00')
        self.table = Item.objects.create(sku='6046', name='TEST TABLE', description='TEST TABLE', price='150.00', on_hand='3',
                                         tax_rate='7.000', average_cost='60.00')
        self.lamp = Item.objects.create(sku='6047', name='TEST LAMP', description='TEST LAMP', price='50.00', on_hand='3',
                                        tax_rate='7.000', average_cost='20.00')
        self.customer = Customer.objects.create(source_id='ret', full_name='TEST RETURNS', charge_account=True)
        DiscountReason.objects.bulk_create([DiscountReason(name=name) for name in ('Sale', 'Custom', 'Defect')])

    def save(self, lines, payments, action='complete', token=None, version=0, customer=None, **extra):
        token = token or str(uuid.uuid4())
        data = dict(token=token, version=version, action=action, customer_id=(customer or self.customer).pk, lines=lines,
                    payments=payments, **extra)
        return save_document(data, 'sale', token)

    def sell(self, qty='2', item=None, payments=None):
        item = item or self.chair
        price = D(item.price)
        total = (price * D(qty) * D('1.07')).quantize(D('0.01'))
        doc = self.save([dict(item_id=item.pk, quantity=qty, price=str(price))], payments or [dict(method='cash', amount=str(total))])
        Document.objects.filter(pk=doc.pk).update(completed_at=SALE_DAY)
        return Document.objects.get(pk=doc.pk)

    def returned(self, original, qty='-1', **extra):
        line = original.lines.first() if isinstance(original, Document) else original
        return {**dict(returned_from_line=line.pk, item_id=line.item_id, quantity=qty, price=str(line.price)), **extra}

    def at_return_day(self, doc):
        Document.objects.filter(pk=doc.pk).update(completed_at=RETURN_DAY)
        return Document.objects.get(pk=doc.pk)

    def on_hand(self, item):
        return Item.objects.get(pk=item.pk).on_hand

    def balance(self):
        return Customer.objects.get(pk=self.customer.pk).account_balance

    def daily(self, d):
        report = sales_tax.build_daily(day(d))
        return report.totals, {r['number']: r for g in report.groups for r in g.rows}


class AcceptReturnTests(ReturnTestCase):
    def test_cash_refund(self):
        sale = self.sell('2')
        self.assertEqual((sale.total, self.on_hand(self.chair)), (D('214.00'), D('3')))
        ret = self.at_return_day(self.save([self.returned(sale)], [dict(method='cash', amount='-107.00')]))
        line = ret.lines.get()
        self.assertEqual((line.quantity, line.price, line.tax_rate, line.returned_from_line_id), (D('-1'), D('100.00'), D('7'), sale.lines.get().pk))
        self.assertEqual((ret.subtotal, ret.tax, ret.total, ret.tendered, ret.change), (D('-100.00'), D('-7.00'), D('-107.00'), D('-107.00'), D('0.00')))
        self.assertEqual(ret.payment_breakdown, [{'method': 'cash', 'amount': '-107.00'}])
        self.assertEqual(line.unit_cost, sale.lines.get().unit_cost)  # the sale's cost comes back off
        self.assertEqual(self.on_hand(self.chair), D('4'))  # back in stock
        self.assertEqual(StockMovement.objects.get(document=ret).quantity, D('1'))
        totals, rows = self.daily(RETURN_DAY.date())
        row = rows[ret.numbered_label]
        self.assertEqual((row['received'], row['cash'], row['tax'], row['new_sale'], row['balance']), (D('-107.00'), D('-107.00'), D('-7.00'), D('-107.00'), D('0.00')))
        # Sale day + return day net to one chair sold.
        report = sales_tax.build_daily(ny_periods.resolve('custom', '', SALE_DAY.date(), SALE_DAY.date(), RETURN_DAY.date()))
        self.assertEqual((report.totals['received'], report.totals['tax'], report.totals['cash']), (D('107.00'), D('7.00'), D('107.00')))
        st = sales_tax.build(ny_periods.resolve('custom', '', SALE_DAY.date(), SALE_DAY.date(), RETURN_DAY.date()), detailed=True)
        self.assertEqual((st.gross, st.taxable, st.tax, st.total), (D('100.00'), D('100.00'), D('7.00'), D('107.00')))
        gs = general_sales.build(ny_periods.resolve('custom', '', SALE_DAY.date(), SALE_DAY.date(), RETURN_DAY.date()))
        self.assertEqual((gs.totals['quantity'], gs.totals['amount'], gs.totals['cost']), (D('1'), D('100.00'), D('40.00')))

    def test_card_refund_even_when_paid_cash_and_cash_refund_of_card_sale(self):
        sale = self.sell('1', payments=[dict(method='cash', amount='107.00')])
        ret = self.at_return_day(self.save([self.returned(sale)], [dict(method='credit', amount='-107.00')]))
        card_sale = self.sell('1', item=self.table, payments=[dict(method='credit', amount='160.50')])
        cash_back = self.at_return_day(self.save([self.returned(card_sale)], [dict(method='cash', amount='-160.50')]))
        totals, rows = self.daily(RETURN_DAY.date())
        self.assertEqual((rows[ret.numbered_label]['card'], rows[ret.numbered_label]['cash']), (D('-107.00'), D('0')))
        self.assertEqual((rows[cash_back.numbered_label]['cash'], rows[cash_back.numbered_label]['card']), (D('-160.50'), D('0')))
        self.assertEqual((totals['received'], totals['tax']), (D('-267.50'), D('-17.50')))

    def test_refund_to_charge_account_is_a_credit(self):
        sale = self.sell('1')
        ret = self.at_return_day(self.save([self.returned(sale)], [dict(method='account', amount='-107.00')]))
        self.assertEqual(self.balance(), D('-107.00'))
        totals, rows = self.daily(RETURN_DAY.date())
        row = rows[ret.numbered_label]
        self.assertEqual((row['received'], row['account'], row['tax']), (D('0'), D('-107.00'), D('0')))  # no money left the drawer
        # The credit is spent on the next sale charged to the account.
        later = self.save([dict(item_id=self.lamp.pk, quantity='1', price='50.00')], [dict(method='account', amount='53.50')])
        self.assertEqual(self.balance(), D('-53.50'))
        texts = self.printed(ret)
        self.assertIn('Refund given (Account): $107.00', texts)
        self.assertNotIn('Signature', texts)  # nothing charged
        self.assertIn('Account Balance: -$107.00', texts)
        self.assertTrue(later.pk)

    def test_exchange_customer_pays_the_difference(self):
        sale = self.sell('1')
        ex = self.at_return_day(self.save([self.returned(sale), dict(item_id=self.table.pk, quantity='1', price='150.00')],
                                          [dict(method='cash', amount='60.00')]))
        self.assertEqual((ex.subtotal, ex.tax, ex.total, ex.change), (D('50.00'), D('3.50'), D('53.50'), D('6.50')))
        self.assertEqual((self.on_hand(self.chair), self.on_hand(self.table)), (D('5'), D('2')))
        totals, rows = self.daily(RETURN_DAY.date())
        row = rows[ex.numbered_label]
        self.assertEqual((row['received'], row['cash'], row['tax'], row['new_sale']), (D('53.50'), D('53.50'), D('3.50'), D('53.50')))
        st = sales_tax.build(day(RETURN_DAY.date()))
        self.assertEqual((st.gross, st.taxable, st.tax, st.total), (D('50.00'), D('50.00'), D('3.50'), D('53.50')))

    def test_exchange_store_refunds_the_difference_split(self):
        sale = self.sell('2')
        ex = self.at_return_day(self.save([self.returned(sale, '-2'), dict(item_id=self.lamp.pk, quantity='1', price='50.00')],
                                          [dict(method='cash', amount='-60.50'), dict(method='credit', amount='-100.00')]))
        self.assertEqual((ex.subtotal, ex.tax, ex.total, ex.payment_method), (D('-150.00'), D('-10.50'), D('-160.50'), 'split'))
        self.assertEqual((self.on_hand(self.chair), self.on_hand(self.lamp)), (D('5'), D('2')))
        totals, rows = self.daily(RETURN_DAY.date())
        row = rows[ex.numbered_label]
        self.assertEqual((row['received'], row['cash'], row['card'], row['tax']), (D('-160.50'), D('-60.50'), D('-100.00'), D('-10.50')))
        gs = general_sales.build(day(RETURN_DAY.date()))
        self.assertEqual((gs.totals['quantity'], gs.totals['amount']), (D('-1'), D('-150.00')))
        texts = self.printed(ex)
        self.assertIn('Return Item - Receipt# ' + sale.number, texts)
        self.assertIn('Refund given (Cash): $60.50', texts)
        self.assertIn('Refund given (Credit card): $100.00', texts)
        self.assertIn('RECEIPT TOTAL: -$160.50', texts)
        self.assertNotIn('Change Given', texts)

    def test_even_exchange_needs_no_payment(self):
        sale = self.sell('1')
        ex = self.save([self.returned(sale), dict(item_id=self.chair.pk, quantity='1', price='100.00')], [dict(method='', amount='')])
        self.assertEqual((ex.total, ex.payment_breakdown, ex.payment_method), (D('0.00'), [], ''))
        self.assertEqual(self.on_hand(self.chair), D('4'))  # one back, one out

    def test_cap_at_quantity_sold_on_that_receipt(self):
        sale = self.sell('2')
        with self.assertRaisesMessage(WorkflowError, 'Only 2 of TEST CHAIR sold on Receipt'):
            self.save([self.returned(sale, '-3')], [dict(method='cash', amount='-321.00')])
        with self.assertRaisesMessage(WorkflowError, 'Only 2 of TEST CHAIR sold'):
            self.save([self.returned(sale, '-2'), self.returned(sale, '-1')], [dict(method='cash', amount='-321.00')])
        # earlier returns count. 2 sold, 1 back already -> 1 left.
        first = self.save([self.returned(sale, '-1')], [dict(method='cash', amount='-107.00')])
        with self.assertRaisesMessage(WorkflowError, f'Only 1 of TEST CHAIR can still be returned from {sale.numbered_label}: 1 of the 2 sold already came back.'):
            self.save([self.returned(sale, '-2')], [dict(method='cash', amount='-214.00')])
        self.save([self.returned(sale, '-1')], [dict(method='cash', amount='-107.00')])
        with self.assertRaisesMessage(WorkflowError, 'Only 0 of TEST CHAIR can still be returned'):
            self.save([self.returned(sale, '-1')], [dict(method='cash', amount='-107.00')])
        # A cancelled return gives its quantity back; a held one doesn't use any.
        save_document(dict(action='cancel', version=first.version), 'sale', first.token)
        self.save([self.returned(sale, '-1')], [], action='hold')
        self.save([self.returned(sale, '-1')], [dict(method='cash', amount='-107.00')])

    def test_cap_counts_quickbooks_returns_of_the_same_item(self):
        """QB links a return to the receipt (DocRefSID -> Line.returned_from_receipt), not the line: QB returns of an
        item are pooled across that receipt's lines of the same item."""
        qb = Document.objects.create(kind='sale', status='completed', source_id='qb-cap', source_number=4100, receipt_type=0,
                                     customer=self.customer, customer_name='TEST RETURNS', completed_at=SALE_DAY, total=D('160.50'))
        a = Line.objects.create(document=qb, position=0, source_id='qb-cap:0', item=self.lamp, sku='6047', item_name='TEST LAMP',
                                description='TEST LAMP', quantity=2, price=D('50.00'), tax_rate=7)
        b = Line.objects.create(document=qb, position=1, source_id='qb-cap:1', item=self.lamp, sku='6047', item_name='TEST LAMP',
                                description='TEST LAMP', quantity=1, price=D('50.00'), tax_rate=7)
        other = Line.objects.create(document=qb, position=2, source_id='qb-cap:2', item=self.table, sku='6046', item_name='TEST TABLE',
                                    description='TEST TABLE', quantity=1, price=D('150.00'), tax_rate=7)
        qb_return = Document.objects.create(kind='sale', status='completed', source_id='qb-ret', source_number=4101, receipt_type=0,
                                            customer=self.customer, completed_at=RETURN_DAY, total=D('-107.00'))
        Line.objects.create(document=qb_return, position=0, source_id='qb-ret:0', item=self.lamp, sku='6047', item_name='TEST LAMP',
                            description='TEST LAMP', quantity=-2, price=D('50.00'), tax_rate=7, returned_from_receipt=qb)
        from .services import return_status
        self.assertEqual(return_status(a), (D('1'), D('1')))   # 3 lamps sold on the receipt, 2 came back in QB
        self.assertEqual(return_status(b), (D('1'), D('0')))
        self.assertEqual(return_status(other), (D('1'), D('0')))
        with self.assertRaisesMessage(WorkflowError, 'Only 1 of TEST LAMP can still be returned from Receipt 4100: 1 of the 2 sold already came back.'):
            self.save([self.returned(a, '-2')], [dict(method='cash', amount='-107.00')])
        self.save([self.returned(a, '-1')], [dict(method='cash', amount='-53.50')])
        self.assertEqual((return_status(a)[0], return_status(b)[0]), (D('0'), D('0')))  # the pool is used up
        lines = {l['id']: l for l in self.client.get(f'/pos/returns/{qb.token}/lines/').json()['lines']}
        self.assertEqual((lines[a.pk]['available'], lines[a.pk]['returned'], lines[other.pk]['available']), ('0', '2', '1'))
        # A cancelled QB return would not count.
        Document.objects.filter(pk=qb_return.pk).update(status='cancelled')
        self.assertEqual(return_status(b), (D('1'), D('0')))

    def test_return_line_takes_original_price_and_rate(self):
        sale = self.sell('1')
        Item.objects.filter(pk=self.chair.pk).update(price='120.00', tax_rate='8.000')
        ret = self.save([self.returned(sale, price='1.00', tax_rate='0', description='changed')], [dict(method='cash', amount='-107.00')])
        line = ret.lines.get()
        self.assertEqual((line.price, line.tax_rate, line.description), (D('100.00'), D('7'), 'TEST CHAIR'))

    def test_rules(self):
        sale = self.sell('1')
        with self.assertRaisesMessage(WorkflowError, 'as a negative number'):
            self.save([self.returned(sale, '1')], [dict(method='cash', amount='107.00')])
        with self.assertRaisesMessage(WorkflowError, 'Cash, Credit card or Account'):
            self.save([self.returned(sale)], [dict(method='check', amount='-107.00')])
        with self.assertRaisesMessage(WorkflowError, 'must add up to the refund due, $107.00'):
            self.save([self.returned(sale)], [dict(method='cash', amount='-100.00')])
        with self.assertRaisesMessage(WorkflowError, 'refund amount'):
            self.save([self.returned(sale)], [dict(method='cash', amount='107.00')])
        with self.assertRaisesMessage(WorkflowError, 'Amount received is less than the total'):
            self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], [dict(method='cash', amount='1.00')])
        with self.assertRaisesMessage(WorkflowError, 'must be a whole number'):
            self.save([dict(item_id=self.chair.pk, quantity='-1', price='100.00')], [dict(method='cash', amount='0')])
        held = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], [], action='hold')
        with self.assertRaisesMessage(WorkflowError, 'can no longer be returned'):
            self.save([self.returned(held)], [dict(method='cash', amount='-107.00')])
        with self.assertRaisesMessage(WorkflowError, 'not a sales order'):
            token = str(uuid.uuid4())
            save_document(dict(token=token, version=0, action='save', customer_id=self.customer.pk, lines=[self.returned(sale)]), 'order', token)
        plain = Customer.objects.create(source_id='plain', full_name='TEST NOT CHARGE')
        with self.assertRaisesMessage(WorkflowError, 'not set up as a charge account'):
            self.save([self.returned(sale)], [dict(method='account', amount='-107.00')], customer=plain)

    def test_original_is_protected_and_cancelling_the_return_reverses_it(self):
        sale = self.sell('2')
        ret = self.save([self.returned(sale)], [dict(method='cash', amount='-107.00')])
        self.assertEqual(self.on_hand(self.chair), D('4'))
        with self.assertRaisesMessage(WorkflowError, f'returned on {ret.numbered_label}. Cancel that return first'):
            save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)
        with self.assertRaisesMessage(WorkflowError, 'cannot be removed'):
            self.save([dict(item_id=self.table.pk, quantity='1', price='150.00')], [dict(method='cash', amount='160.50')],
                      token=str(sale.token), version=sale.version)
        save_document(dict(action='cancel', version=ret.version), 'sale', ret.token)
        self.assertEqual(self.on_hand(self.chair), D('3'))
        save_document(dict(action='cancel', version=sale.version), 'sale', sale.token)  # allowed once the return is cancelled
        self.assertEqual(self.on_hand(self.chair), D('5'))

    def test_editing_a_saved_return_keeps_its_link_and_cap(self):
        sale = self.sell('2')
        ret = self.save([self.returned(sale)], [dict(method='cash', amount='-107.00')])
        line = ret.lines.get()
        edited = self.save([dict(id=line.pk, quantity='-2', returned_from_line=999999)], [dict(method='cash', amount='-214.00')],
                           token=str(ret.token), version=ret.version)
        self.assertEqual((edited.total, edited.lines.get().returned_from_line_id, self.on_hand(self.chair)), (D('-214.00'), sale.lines.get().pk, D('5')))
        with self.assertRaisesMessage(WorkflowError, 'Only 2'):
            self.save([dict(id=line.pk, quantity='-3')], [dict(method='cash', amount='-321.00')], token=str(ret.token), version=edited.version)
        # Its own saved lines don't count against it, on the server or in the pick list opened while editing it.
        data = self.client.get(f'/pos/returns/{sale.token}/lines/', {'exclude': str(ret.token)}).json()
        self.assertEqual((data['lines'][0]['available'], data['lines'][0]['returned']), ('2', '0'))
        self.assertEqual(self.client.get(f'/pos/returns/{sale.token}/lines/').json()['lines'][0]['available'], '0')
        self.assertEqual(self.client.get(f'/pos/sale/{ret.token}/').context['initial']['lines'][0]['return_limit'], '2')

    def test_return_from_imported_qb_receipt_and_full_wipe(self):
        qb = Document.objects.create(kind='sale', status='completed', source_id='qb-1', source_number=783, receipt_type=0,
                                     customer=self.customer, customer_name='TEST RETURNS', completed_at=SALE_DAY,
                                     subtotal=D('52.00'), tax=D('3.64'), total=D('55.64'))
        Line.objects.create(document=qb, position=0, source_id='qb-1-0', item=self.lamp, sku='6047', item_name='TEST LAMP',
                            description='TEST LAMP', quantity=2, price=D('26.00'), tax_rate=7, unit_cost=D('11.00'))
        ret = self.save([self.returned(qb, '-2')], [dict(method='credit', amount='-55.64')])
        self.assertEqual((ret.total, self.on_hand(self.lamp)), (D('-55.64'), D('5')))
        self.assertIn('Return Item - Receipt# 783', self.printed(ret))
        # rebuild_from_backup's wipe (WIPE_ORDER: Tender, Line, StockMovement,..., Document): RESTRICT lets a return
        # and its original line be deleted together.
        Line.objects.all().delete()
        StockMovement.objects.all().delete()
        Document.objects.all().delete()
        self.assertFalse(Line.objects.exists())

    def test_database_refuses_unlinked_negative_app_lines(self):
        doc = Document.objects.create(kind='sale', status='held', customer=self.customer)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Line.objects.create(document=doc, position=0, description='x', quantity=-1, price=1)

    def printed(self, doc):
        texts = []
        original_text, original_columns, original_wrap = Receipt.text, Receipt.columns, Receipt.wrap
        with mock.patch.object(Receipt, 'text', autospec=True, side_effect=lambda self, t, *a, **k: (texts.append(t), original_text(self, t, *a, **k))[1]), \
                mock.patch.object(Receipt, 'wrap', autospec=True, side_effect=lambda self, t, *a, **k: (texts.append(t), original_wrap(self, t, *a, **k))[1]), \
                mock.patch.object(Receipt, 'columns', autospec=True, side_effect=lambda self, cells, *a, **k: (texts.append(' '.join(c[0] for c in cells)), original_columns(self, cells, *a, **k))[1]):
            render_receipt(Document.objects.get(pk=doc.pk))
        return '\n'.join(texts)


class AcceptReturnViewTests(ReturnTestCase):
    def test_lookup_lines_and_register(self):
        sale = self.sell('2')
        found = self.client.get('/pos/returns/lookup/', {'q': sale.number}).json()['results']
        self.assertEqual([r['token'] for r in found], [str(sale.token)])
        by_name = self.client.get('/pos/returns/lookup/', {'q': 'test returns'}).json()['results']
        self.assertIn(str(sale.token), [r['token'] for r in by_name])
        data = self.client.get(f'/pos/returns/{sale.token}/lines/').json()
        self.assertEqual((data['number'], data['customer']['id'], len(data['lines'])), (sale.number, self.customer.pk, 1))
        self.assertEqual((data['lines'][0]['sold'], data['lines'][0]['price'], data['lines'][0]['tax_rate']), ('2', '100.00', '7'))
        page = self.client.get(f'/pos/sale/new/?return_from={sale.token}')
        self.assertEqual(page.context['initial']['return_from'], str(sale.token))
        self.assertContains(page, 'id="return-dialog"')
        detail = self.client.get(f'/pos/sale/{sale.token}/')
        self.assertContains(detail, f'href="/pos/sale/new/?return_from={sale.token}">Accept Return</a>')
        self.assertContains(self.client.get('/pos/history/'), 'href="/pos/sale/new/?accept_return=1">Accept Return</a>')
        ret = self.save([self.returned(sale)], [dict(method='cash', amount='-107.00')])
        lines = self.client.get(f'/pos/sale/{ret.token}/').context['initial']['lines']
        self.assertEqual((lines[0]['returned_from_line'], lines[0]['return_receipt'], lines[0]['return_limit'], lines[0]['quantity']),
                         (sale.lines.get().pk, sale.number, '2', '-1'))

    def test_lines_refused_for_held_or_deposit_receipts(self):
        held = self.save([dict(item_id=self.chair.pk, quantity='1', price='100.00')], [], action='hold')
        self.assertEqual(self.client.get(f'/pos/returns/{held.token}/lines/').status_code, 400)
        deposit = Document.objects.create(kind='sale', status='completed', source_id='dep', source_number=5, receipt_type=2,
                                          customer=self.customer, total=D('10'))
        self.assertEqual(self.client.get(f'/pos/returns/{deposit.token}/lines/').status_code, 400)
        self.assertEqual(self.client.get('/pos/returns/lookup/', {'q': '5'}).json()['results'], [])
