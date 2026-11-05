# SPDX-License-Identifier: AGPL-3.0-or-later
"""held receipts hold their stock, with a hold time.

Only a hold made in the app for a real customer reserves its items; set signs (no customer), QUOTE customers and
held receipts imported from QuickBooks reserve nothing. Reservations are not stock movements: On Hand stays physical
and Available = On Hand - open order qty - held. Expired holds release the stock and stay on the list marked Expired.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from customers.models import Customer
from purchasing import reorder
from purchasing.models import Vendor
from . import holds
from .models import Document, Item, Line, StockMovement
from .services import save_document, WorkflowError


def hold_payload(customer, *lines, **extra):
    data = dict(token=str(uuid.uuid4()), action='hold', version=0, customer_id=customer.pk,
                lines=[dict(item_id=item.pk, quantity=str(qty), price='10.00', tax_rate='7') for item, qty in lines])
    data.update(extra)
    return data


def hold(customer, *lines, **extra):
    data = hold_payload(customer, *lines, **extra)
    return save_document(data, 'sale', data['token'])


def resave(doc, action, *lines, **extra):
    data = dict(token=str(doc.token), action=action, version=doc.version, customer_id=extra.pop('customer_id', doc.customer_id),
                lines=[dict(id=line_pk, item_id=item.pk, quantity=str(qty), price='10.00', tax_rate='7') for line_pk, item, qty in lines])
    if action == 'complete':
        data['payments'] = [dict(method='cash', amount=extra.pop('amount', '999.00'))]
    data.update(extra)
    return save_document(data, 'sale', doc.token)


class HeldStockTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(source_id='C1', full_name='DANA SAMPLE', phone='518-555-0100')
        self.quote = Customer.objects.create(source_id='C2', first_name='QUOTE', last_name='QUOTE')
        self.chair = Item.objects.create(sku='CHAIR', name='CHAIR', description='Test chair', price='10.00', on_hand='5', tax_rate='7')
        self.table = Item.objects.create(sku='TABLE', name='TABLE', description='Test table', price='10.00', on_hand='2', tax_rate='7')

    def held(self, item):
        return holds.held_by_item([item.pk]).get(item.pk, Decimal('0'))

    def available(self, item):
        return holds.available_by_item([Item.objects.get(pk=item.pk)])[item.pk]

    # --- who reserves ---
    def test_hold_for_a_real_customer_reserves_without_moving_stock(self):
        doc = hold(self.customer, (self.chair, 2), (self.table, 1))
        self.assertEqual((doc.status, doc.holds_stock, doc.hold_expired_at), ('held', True, None))
        self.assertEqual(doc.hold_date, timezone.localdate() + timedelta(days=7))  # default preset: 1 week
        self.assertEqual(Item.objects.get(pk=self.chair.pk).on_hand, Decimal('5'))  # On Hand stays physical
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual((self.held(self.chair), self.available(self.chair)), (Decimal('2'), Decimal('3')))
        self.assertEqual((self.held(self.table), self.available(self.table)), (Decimal('1'), Decimal('1')))

    def test_quote_customer_and_no_customer_and_imported_do_not_reserve(self):
        quote = hold(self.quote, (self.chair, 2))
        self.assertFalse(quote.holds_stock)
        self.assertEqual(quote.hold_label, 'No stock set aside')
        # Set signs come over from QB with no customer; imported held receipts keep holds_stock False (no import change).
        sign = Document.objects.create(kind='sale', status='held', customer=None, customer_name='', source_id='QB-1', subtotal=0, total=0)
        Line.objects.create(document=sign, position=0, item=self.chair, quantity='1', price='10.00', tax_rate='7')
        imported = Document.objects.create(kind='sale', status='held', customer=self.customer, customer_name='DANA SAMPLE', source_id='QB-2', subtotal=0, total=0)
        Line.objects.create(document=imported, position=0, item=self.chair, quantity='1', price='10.00', tax_rate='7')
        self.assertEqual(self.held(self.chair), Decimal('0'))
        self.assertEqual(self.available(self.chair), Decimal('5'))
        # (wording): the Held Receipts list and details say "No stock set aside", never "Not held".
        self.assertEqual(imported.hold_label, 'No stock set aside')
        page = self.client.get('/pos/held/?period=all')
        self.assertContains(page, 'No stock set aside')
        self.assertNotContains(page, 'Not held')
        details = self.client.get(f'/pos/held/{imported.token}/details/')
        self.assertContains(details, 'no stock set aside — brought over from QuickBooks')
        self.assertNotContains(details, 'not held')
        for name in ('QUOTE jones', 'QUOTE, PETTIT', 'WARD QUOTE QUOTE QUOTE', 'quote'):
            self.assertFalse(holds.reserves_stock(Customer(full_name=name)), name)
        self.assertTrue(holds.reserves_stock(Customer(full_name='MISQUOTED EXAMPLE')))  # only the word QUOTE
        self.assertFalse(holds.reserves_stock(None))

    def test_non_inventory_lines_and_special_lines_do_not_reserve(self):
        service = Item.objects.create(sku='DELIV', name='DELIVERY', description='Delivery', price='50.00', on_hand=None, tax_rate='7', item_type='noninventory')
        doc = hold(self.customer, (service, 1), (self.chair, 1))
        self.assertTrue(doc.holds_stock)
        self.assertEqual(holds.held_by_item([service.pk, self.chair.pk]), {self.chair.pk: Decimal('1')})
        # Review fix: the Item List (annotate_held) uses the same inventory-only rule as Item Detail.
        listed = dict(holds.annotate_held(Item.objects.filter(pk__in=[service.pk, self.chair.pk])).values_list('pk', 'held'))
        self.assertEqual((listed[service.pk], listed[self.chair.pk]), (Decimal('0'), Decimal('1')))
        rows = self.client.get('/purchasing/items/?format=rows&offset=0&sort=-held').json()['rows']
        cells = {row['cells'][0]: row['cells'][3] for row in rows}
        self.assertEqual((cells['DELIVERY'], cells['CHAIR']), ('0', '1'))

    # --- lifecycle ---
    def test_complete_sells_once_and_ends_the_hold(self):
        doc = hold(self.customer, (self.chair, 2))
        line = doc.lines.get()
        done = resave(doc, 'complete', (line.pk, self.chair, 2), amount='21.40')
        self.assertEqual((done.status, done.holds_stock, done.hold_until), ('completed', False, None))
        self.assertEqual(Item.objects.get(pk=self.chair.pk).on_hand, Decimal('3'))  # one deduction
        self.assertEqual(StockMovement.objects.get(document=done).quantity, Decimal('-2'))
        self.assertEqual((self.held(self.chair), self.available(self.chair)), (Decimal('0'), Decimal('3')))

    def test_cancel_and_delete_release(self):
        doc = hold(self.customer, (self.chair, 2))
        self.assertEqual(self.available(self.chair), Decimal('3'))
        save_document(dict(token=str(doc.token), action='cancel', version=doc.version), 'sale', doc.token)
        self.assertEqual(Document.objects.get(pk=doc.pk).status, 'cancelled')
        self.assertEqual((self.available(self.chair), Item.objects.get(pk=self.chair.pk).on_hand), (Decimal('5'), Decimal('5')))
        other = hold(self.customer, (self.table, 1))
        self.assertEqual(self.available(self.table), Decimal('1'))
        response = self.client.post(f'/pos/held/{other.token}/delete/', {'next': ''})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.available(self.table), Decimal('2'))

    def test_editing_a_held_receipt_adjusts_the_hold_and_keeps_its_time(self):
        doc = hold(self.customer, (self.chair, 2), hold_until='3d')
        until = doc.hold_until
        line = doc.lines.get()
        edited = resave(doc, 'hold', (line.pk, self.chair, 4))  # no hold_until sent = keep
        self.assertEqual((edited.hold_until, edited.holds_stock), (until, True))
        self.assertEqual(self.held(self.chair), Decimal('4'))
        edited = resave(edited, 'hold', (line.pk, self.chair, 1), hold_until='keep')
        self.assertEqual((edited.hold_until, self.held(self.chair)), (until, Decimal('1')))
        # Switching the receipt to a QUOTE customer stops the hold; switching back restores it.
        edited = resave(edited, 'hold', (line.pk, self.chair, 1), customer_id=self.quote.pk)
        self.assertFalse(edited.holds_stock)
        self.assertEqual(self.held(self.chair), Decimal('0'))

    def test_re_holding_a_receipt_with_no_stock_hold_defaults_to_one_week(self):
        """Review fix: 'Keep the current hold' only for a receipt that already held stock. An imported QB held receipt
        (or a QUOTE hold) re-held for a real customer starts a fresh 1-week hold, never an open-ended one."""
        today = timezone.localdate()
        qb = Document.objects.create(kind='sale', status='held', customer=self.customer, customer_name='DANA SAMPLE',
                                     source_id='QB-9', subtotal=0, total=0)
        qb_line = Line.objects.create(document=qb, position=0, item=self.chair, quantity='1', price='10.00', tax_rate='7')
        page = self.client.get(f'/pos/sale/{qb.token}/')
        self.assertContains(page, 'id="hold-dialog"')
        self.assertNotContains(page, 'Keep the current hold')
        self.assertContains(page, 'value="1w" checked')
        with self.assertRaisesMessage(WorkflowError, 'Choose how long'):
            resave(qb, 'hold', (qb_line.pk, self.chair, 1), hold_until='keep')
        qb.refresh_from_db()
        self.assertEqual((qb.holds_stock, qb.hold_until, qb.source_id), (False, None, 'QB-9'))  # refused: nothing saved
        reheld = resave(qb, 'hold', (qb_line.pk, self.chair, 1))  # nothing sent = the default
        self.assertEqual((reheld.holds_stock, reheld.hold_date, reheld.source_id), (True, today + timedelta(days=7), None))
        self.assertEqual(self.held(self.chair), Decimal('1'))
        # Now it is an app stock hold, so the register offers Keep and keeps its time.
        page = self.client.get(f'/pos/sale/{reheld.token}/')
        self.assertContains(page, 'value="keep" checked')
        self.assertNotContains(page, 'value="1w" checked')
        kept = resave(reheld, 'hold', (qb_line.pk, self.chair, 1), hold_until='keep')
        self.assertEqual(kept.hold_until, reheld.hold_until)
        # A QUOTE hold switched to a real customer also starts at 1 week, whatever time the quote carried.
        quote = hold(self.quote, (self.table, 1), hold_until='none')
        self.assertFalse(quote.holds_stock)
        line = quote.lines.get()
        real = resave(quote, 'hold', (line.pk, self.table, 1), customer_id=self.customer.pk)
        self.assertEqual((real.holds_stock, real.hold_date), (True, today + timedelta(days=7)))
        # An app hold saved with No expiry keeps No expiry on 'keep' (and when nothing is sent).
        forever = hold(self.customer, (self.chair, 1), hold_until='none')
        line = forever.lines.get()
        forever = resave(forever, 'hold', (line.pk, self.chair, 1), hold_until='keep')
        self.assertEqual((forever.holds_stock, forever.hold_until), (True, None))
        forever = resave(forever, 'hold', (line.pk, self.chair, 1))
        self.assertEqual((forever.holds_stock, forever.hold_until, forever.hold_label), (True, None, 'No expiry'))
        page = self.client.get(f'/pos/sale/{forever.token}/')
        self.assertContains(page, 'Keep the current hold (<span id="hold-current">No expiry</span>)')

    def test_presets_custom_date_and_no_expiry(self):
        today = timezone.localdate()
        for code, days in (('1d', 1), ('3d', 3), ('1w', 7), ('2w', 14), ('30d', 30)):
            doc = hold(self.customer, (self.chair, 1), hold_until=code)
            self.assertEqual(doc.hold_date, today + timedelta(days=days), code)
            self.assertEqual(timezone.localtime(doc.hold_until).strftime('%H:%M:%S'), '23:59:59')  # end of the store day
        custom = hold(self.customer, (self.chair, 1), hold_until=str(today + timedelta(days=45)))
        self.assertEqual(custom.hold_date, today + timedelta(days=45))
        self.assertEqual(custom.hold_label, f'Until {custom.hold_date.strftime("%-m/%-d/%Y")}')
        forever = hold(self.customer, (self.chair, 1), hold_until='none')
        self.assertEqual((forever.hold_until, forever.holds_stock, forever.hold_label), (None, True, 'No expiry'))
        with self.assertRaisesMessage(WorkflowError, 'already passed'):
            hold(self.customer, (self.chair, 1), hold_until=str(today - timedelta(days=1)))
        with self.assertRaisesMessage(WorkflowError, 'valid hold-until date'):
            hold(self.customer, (self.chair, 1), hold_until='next tuesday')
        with self.assertRaisesMessage(WorkflowError, 'Choose how long'):
            hold(self.customer, (self.chair, 1), hold_until='keep')  # new hold: nothing to keep
        self.assertEqual(self.held(self.chair), Decimal('7'))

    def test_expiry_releases_and_marks_expired_then_extend_reserves_again(self):
        doc = hold(self.customer, (self.chair, 2), hold_until='1d')
        self.assertEqual(self.available(self.chair), Decimal('3'))
        later = timezone.now() + timedelta(days=2)
        with mock.patch('django.utils.timezone.now', return_value=later):
            self.assertEqual(self.available(self.chair), Decimal('5'))  # lazily expired on read
            doc.refresh_from_db()
            self.assertIsNotNone(doc.hold_expired_at)
            self.assertTrue(doc.hold_expired)
            self.assertEqual(doc.hold_label, f'Expired {doc.hold_date.strftime("%-m/%-d/%Y")}')
            page = self.client.get('/pos/held/')
            self.assertContains(page, 'Expired')
            self.assertContains(page, '>Hold until<')
            rows = self.client.get('/pos/held/?format=rows&offset=0').json()['rows']
            self.assertTrue(rows[0]['cells'][11].startswith('Expired'))
            details = self.client.get(f'/pos/held/{doc.token}/details/')
            self.assertContains(details, 'EXPIRED')
            self.assertContains(details, 'Extend Hold')
            # Re-saving with 'keep' leaves it expired; a new hold time clears the mark and reserves again.
            line = doc.lines.get()
            kept = resave(doc, 'hold', (line.pk, self.chair, 2), hold_until='keep')
            self.assertTrue(kept.hold_expired)
            self.assertEqual(self.available(self.chair), Decimal('5'))
            extended = resave(kept, 'hold', (line.pk, self.chair, 2), hold_until='1w')
            self.assertEqual((extended.hold_expired_at, extended.hold_expired), (None, False))
            self.assertEqual(self.available(self.chair), Decimal('3'))
            # An expired hold can still be finished normally.
            done = resave(extended, 'complete', (line.pk, self.chair, 2), amount='21.40')
            self.assertEqual((done.status, Item.objects.get(pk=self.chair.pk).on_hand), ('completed', Decimal('3')))

    def test_extend_from_the_held_receipts_list(self):
        doc = hold(self.customer, (self.chair, 2), hold_until='1d')
        today = timezone.localdate()
        response = self.client.post(f'/pos/held/{doc.token}/extend/', {'hold_until': '30d', 'next': 'period=all'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('extended=', response['Location'])
        self.assertIn('period=all', response['Location'])
        doc.refresh_from_db()
        self.assertEqual((doc.hold_date, doc.version), (today + timedelta(days=30), 2))
        response = self.client.post(f'/pos/held/{doc.token}/extend/', {'hold_until': 'custom', 'hold_date': str(today + timedelta(days=60))})
        doc.refresh_from_db()
        self.assertEqual(doc.hold_date, today + timedelta(days=60))
        response = self.client.post(f'/pos/held/{doc.token}/extend/', {'hold_until': 'none'})
        doc.refresh_from_db()
        self.assertEqual((doc.hold_until, doc.holds_stock), (None, True))
        response = self.client.post(f'/pos/held/{doc.token}/extend/', {'hold_until': 'custom', 'hold_date': '2001-01-01'})
        self.assertIn('error=', response['Location'])
        # A QUOTE held receipt can be given a hold time, but never holds stock.
        quote = hold(self.quote, (self.chair, 1))
        self.client.post(f'/pos/held/{quote.token}/extend/', {'hold_until': '1w'})
        quote.refresh_from_db()
        self.assertFalse(quote.holds_stock)
        self.assertEqual(self.client.get(f'/pos/held/{quote.token}/extend/').status_code, 405)

    # --- where Available shows ---
    def test_item_detail_item_list_picker_and_register_show_held_and_available(self):
        order = Document.objects.create(kind='order', status='open', customer=self.customer, customer_name='DANA SAMPLE', subtotal=0, total=0)
        Line.objects.create(document=order, position=0, item=self.chair, quantity='1', price='10.00', tax_rate='7')
        hold(self.customer, (self.chair, 2))
        details = self.client.get(f'/purchasing/items/{self.chair.pk}/details/')
        self.assertContains(details, 'Held Qty')
        self.assertContains(details, 'Less: Held for Customers</dt><dd>2<')
        self.assertEqual(details.context['available'], Decimal('2'))  # 5 - 1 on order - 2 held
        self.assertEqual((details.context['held'], details.context['customer_orders']), (Decimal('2'), Decimal('1')))
        page = self.client.get('/purchasing/items/')
        self.assertContains(page, '<th>Held</th>')
        self.assertEqual(page.context['item_scroll_config']['columns'], 17)
        rows = self.client.get('/purchasing/items/?format=rows&offset=0&sort=-held').json()['rows']
        self.assertEqual((rows[0]['cells'][0], rows[0]['cells'][2], rows[0]['cells'][3]), ('CHAIR', '5', '2'))
        self.assertEqual(rows[1]['cells'][3], '0')
        picker = self.client.get('/purchasing/item-picker/', {'q': 'CHAIR'}).json()['results'][0]
        self.assertEqual((picker['on_hand'], picker['available']), ('5', '2'))
        lookup = self.client.get('/pos/lookup/items/?q=CHAIR').json()['results'][0]
        self.assertEqual((lookup['on_hand'], lookup['available']), ('5', '2'))
        edit = self.client.get(f'/purchasing/items/{self.chair.pk}/')
        self.assertContains(edit, 'Held for customers</dt><dd>2<')
        self.assertContains(edit, 'Available</dt><dd>2<')

    def test_register_lines_show_the_same_available_as_inventory(self):
        doc = hold(self.customer, (self.chair, 2))
        other = hold(self.customer, (self.chair, 1))
        from .views import document_data
        line = document_data(doc)['lines'][0]
        self.assertEqual((line['on_hand'], line['available']), ('5', '2'))  # 5 - 2 - 1: every hold counts, this one too
        self.assertEqual(document_data(other)['lines'][0]['available'], '2')
        self.assertEqual(self.available(self.chair), Decimal('2'))  # the Item List / Item Detail figure
        lookup = self.client.get('/pos/lookup/items/?q=CHAIR').json()['results'][0]
        self.assertEqual(lookup['available'], '2')  # a line added on the register shows the same number
        data = document_data(doc)
        self.assertEqual((data['holds_stock'], data['hold_until'], data['hold_expired']), (True, str(doc.hold_date), False))
        page = self.client.get(f'/pos/sale/{doc.token}/')
        self.assertContains(page, 'Holding the items for DANA SAMPLE until')
        self.assertContains(page, 'id="hold-dialog"')
        self.assertContains(page, 'Keep the current hold')
        self.assertContains(page, '<th class="number">Available</th>')
        new = self.client.get('/pos/sale/new/')
        self.assertContains(new, 'value="1w" checked')
        for label in ('1 day', '3 days', '1 week', '2 weeks', '30 days', 'Custom date', 'No expiry'):
            self.assertContains(new, f'> {label}</label>')

    def test_reorder_reminders_subtract_held_under_the_available_rule(self):
        vendor = Vendor.objects.create(code='DR/', company='Test Vendor')
        self.chair.reorder_point = 3
        self.chair.primary_vendor = vendor
        self.chair.save()
        row = next(r for r in reorder.evaluate() if r['item'] == self.chair)
        self.assertEqual((row['held'], row['available'], row['below']), (Decimal('0'), Decimal('5'), False))
        hold(self.customer, (self.chair, 3))
        row = next(r for r in reorder.evaluate() if r['item'] == self.chair)
        self.assertEqual((row['held'], row['available'], row['projected'], row['below']), (Decimal('3'), Decimal('2'), Decimal('2'), True))
        row = next(r for r in reorder.evaluate('on_hand') if r['item'] == self.chair)
        self.assertFalse(row['below'])  # the on-hand rule ignores commitments and holds, as before
        page = self.client.get('/purchasing/reorder/')
        self.assertContains(page, '<th class="num">Held</th>')

    def test_held_list_shows_hold_until_column(self):
        doc = hold(self.customer, (self.chair, 2), hold_until='none')
        page = self.client.get('/pos/held/')
        # Review fix: the stale-response guard for fast row switching must be live code, not swallowed by a comment.
        script = page.content.decode()
        guard = 'if(token!==selected?.token)return;'
        self.assertEqual(script.count(guard), 2)  # after the details load, and in its error path
        for line in script.splitlines():
            if guard in line:
                self.assertNotIn('//', line.split(guard)[0], line)
        self.assertLess(script.index(guard), script.index('panel.innerHTML=html;'))
        self.assertContains(page, '>Hold until<')
        self.assertContains(page, 'No expiry')
        rows = self.client.get('/pos/held/?format=rows&offset=0').json()['rows']
        self.assertEqual(rows[0]['cells'][11], 'No expiry')
        details = self.client.get(f'/pos/held/{doc.token}/details/')
        self.assertContains(details, 'Hold until:')
        self.assertContains(details, 'no expiry — items held for DANA SAMPLE')
        self.assertContains(details, '/extend/')
