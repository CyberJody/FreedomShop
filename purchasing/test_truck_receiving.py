# SPDX-License-Identifier: AGPL-3.0-or-later
"""Truck Receiving. Scan each piece's label off the truck; Receive Items posts one ordinary
receiving voucher per PO on the truck. Pieces not scanned stay due; removed pieces are not received."""
import json
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accounts.roles import ADMIN_GROUP, REGISTER_GROUP, group
from sales.models import Item
from sales.services import WorkflowError
from . import piece_labels, truck
from .models import PieceLabel, PurchaseDocument, TruckScan, TruckSession, Vendor
from .services import save_purchase

AUTH = ['django.contrib.sessions.middleware.SessionMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware']


def header(vendor):
    return dict(token=uuid.uuid4(), version=0, vendor=vendor, status='open', notes='', terms='', ship_date=None,
                payment_due=None, invoice_number='')


class TruckData(TestCase):
    def setUp(self):
        self.v1 = Vendor.objects.create(code='CHE', company='SAMPLE FURNITURE SUPPLY')
        self.v2 = Vendor.objects.create(code='AMI', company='DEMO OAK WORKS')
        self.chair = Item.objects.create(sku='2001', name='CHAIR-OAK', description='OAK SIDE CHAIR', price=300, on_hand=0,
                                         average_cost=100, order_cost=100, primary_vendor=self.v1, tax_rate=7)
        self.table = Item.objects.create(sku='2002', name='TABLE-OAK', description='OAK TABLE', price=900, on_hand=1,
                                         average_cost=400, order_cost=400, primary_vendor=self.v1, tax_rate=7)
        self.bench = Item.objects.create(sku='2003', name='BENCH-MAPLE', description='MAPLE BENCH', price=500, on_hand=0,
                                         average_cost=200, order_cost=200, primary_vendor=self.v2, tax_rate=7)
        self.po1 = save_purchase(header(self.v1), [dict(item=self.chair, quantity=Decimal('4'), cost=Decimal('100')),
                                                   dict(item=self.table, quantity=Decimal('2'), cost=Decimal('400'))], 'po', 'save')
        self.po2 = save_purchase(header(self.v2), [dict(item=self.bench, quantity=Decimal('3'), cost=Decimal('200'))], 'po', 'save')
        piece_labels.ensure_codes(self.po1)
        piece_labels.ensure_codes(self.po2)
        self.chair_line, self.table_line = self.po1.lines.order_by('position')
        self.bench_line = self.po2.lines.get()
        self.session = TruckSession.objects.create()

    def code(self, line, piece):
        return PieceLabel.objects.get(line=line, piece=piece).code


class ScanTests(TruckData):
    def test_ok_scan_counts_one_piece(self):
        result = truck.scan(self.session, self.code(self.chair_line, 3))
        self.assertEqual(result['kind'], 'ok')
        self.assertEqual((result['item'], result['po'], result['piece'], result['pieces'], result['on_truck']),
                         ('CHAIR-OAK', self.po1.number, 3, 4, 1))
        self.assertEqual(truck.summary(self.session), dict(pieces=1, pos=1, vendors=1))

    def test_qr_url_and_lowercase_are_accepted(self):
        code = self.code(self.chair_line, 1)
        self.assertEqual(truck.scan(self.session, f'https://labels.example.org/?wc={code}')['kind'], 'ok')   # older label URL
        self.assertEqual(truck.scan(self.session, f'https://example.com/?wc={self.code(self.chair_line, 3)}')['kind'], 'ok')  # current label URL
        self.assertEqual(truck.scan(self.session, self.code(self.chair_line, 2).lower())['kind'], 'ok')

    def test_repeat_is_not_counted(self):
        code = self.code(self.chair_line, 1)
        truck.scan(self.session, code)
        result = truck.scan(self.session, code)
        self.assertEqual(result['kind'], 'repeat')
        self.assertEqual(self.session.scans.count(), 1)

    def test_not_a_code_and_bad_check_character(self):
        self.assertEqual(truck.scan(self.session, '1234567890')['message'], 'Not a piece code')
        code = self.code(self.chair_line, 1)
        last = code[-1]
        wrong = code[:-1] + ('2' if last != '2' else '3')
        self.assertEqual(truck.scan(self.session, wrong)['message'], 'Not a piece code')

    def test_unknown_code(self):
        from .piece_codes import new_code
        code = new_code()
        while PieceLabel.objects.filter(code=code).exists():
            code = new_code()
        result = truck.scan(self.session, code)
        self.assertEqual((result['kind'], result['message']), ('error', 'No label with this code'))

    def test_cancelled_po(self):
        save_purchase(dict(token=self.po2.token, version=self.po2.version), [], 'po', 'cancel')
        result = truck.scan(self.session, self.code(self.bench_line, 1))
        self.assertEqual(result['kind'], 'error')
        self.assertIn(f'{self.po2.number} was cancelled', result['message'])

    def test_fully_received_line(self):
        save_purchase(header(self.v1), [dict(item=self.table, quantity=Decimal('2'), cost=Decimal('400'), source_line=self.table_line.pk)],
                      'voucher', 'post', self.po1)
        result = truck.scan(self.session, self.code(self.table_line, 1))
        self.assertEqual(result['kind'], 'error')
        self.assertIn('already fully received', result['message'])
        self.assertIn(self.po1.number, result['message'])

    def test_more_pieces_than_still_due(self):
        # 3 of 4 chairs came in on a manual voucher: only one chair label can still go on a truck.
        save_purchase(header(self.v1), [dict(item=self.chair, quantity=Decimal('3'), cost=Decimal('100'), source_line=self.chair_line.pk)],
                      'voucher', 'post', self.po1)
        self.assertEqual(truck.scan(self.session, self.code(self.chair_line, 4))['kind'], 'ok')
        result = truck.scan(self.session, self.code(self.chair_line, 1))
        self.assertEqual(result['kind'], 'error')
        self.assertIn('already scanned on this truck', result['message'])

    def test_piece_on_another_truck(self):
        code = self.code(self.chair_line, 1)
        truck.scan(self.session, code)
        other = TruckSession.objects.create()
        result = truck.scan(other, code)
        self.assertEqual(result['kind'], 'error')
        self.assertIn(f'Already scanned on {self.session.number}', result['message'])
        truck.receive(self.session)
        result = truck.scan(other, code)
        voucher = PurchaseDocument.objects.get(kind='voucher')
        self.assertEqual(result['message'], f'Already received on {self.session.number}, voucher {voucher.number}')

    def test_removal_frees_the_label(self):
        code = self.code(self.chair_line, 2)
        truck.scan(self.session, code)
        scan = self.session.scans.get()
        with self.assertRaises(WorkflowError):
            truck.remove(self.session, '   ', scan_id=scan.pk)   # a reason is required
        self.assertEqual(truck.remove(self.session, 'damaged, going back', scan_id=scan.pk), 1)
        scan.refresh_from_db()
        self.assertEqual((scan.removed, scan.removed_reason), (True, 'damaged, going back'))
        other = TruckSession.objects.create()
        self.assertEqual(truck.scan(other, code)['kind'], 'ok')

    def test_remove_all_on_a_line(self):
        for piece in (1, 2, 3):
            truck.scan(self.session, self.code(self.chair_line, piece))
        truck.scan(self.session, self.code(self.table_line, 1))
        self.assertEqual(truck.remove(self.session, 'wrong finish', line_id=self.chair_line.pk), 3)
        self.assertEqual(truck.summary(self.session)['pieces'], 1)

    def test_scan_on_received_truck_is_refused(self):
        truck.scan(self.session, self.code(self.chair_line, 1))
        truck.receive(self.session)
        result = truck.scan(self.session, self.code(self.chair_line, 2))
        self.assertEqual(result['kind'], 'error')
        self.assertIn('already received', result['message'])


class ReceiveTests(TruckData):
    def test_one_voucher_per_po_with_the_scanned_quantities(self):
        for piece in (1, 2, 3):
            truck.scan(self.session, self.code(self.chair_line, piece))
        truck.scan(self.session, self.code(self.table_line, 1))
        for piece in (1, 2):
            truck.scan(self.session, self.code(self.bench_line, piece))
        truck.remove(self.session, 'damaged, going back', scan_id=self.session.scans.get(label__line=self.chair_line, label__piece=3).pk)
        vouchers = truck.receive(self.session)
        self.assertEqual(len(vouchers), 2)
        by_po = {v.source_po_id: v for v in vouchers}
        v1, v2 = by_po[self.po1.pk], by_po[self.po2.pk]
        self.assertEqual((v1.status, v1.vendor_id, v2.status, v2.vendor_id), ('posted', self.v1.pk, 'posted', self.v2.pk))
        self.assertEqual({l.item_id: l.quantity for l in v1.lines.all()}, {self.chair.pk: Decimal('2'), self.table.pk: Decimal('1')})
        self.assertEqual({l.item_id: l.quantity for l in v2.lines.all()}, {self.bench.pk: Decimal('2')})
        self.assertEqual(v1.lines.get(item=self.chair).cost, Decimal('100.00'))
        self.assertIn(self.session.number, v1.notes)
        for obj in (self.chair_line, self.table_line, self.bench_line, self.chair, self.table, self.bench, self.po1, self.po2, self.session):
            obj.refresh_from_db()
        # Un-scanned (and removed) pieces stay due on their POs.
        self.assertEqual((self.chair_line.received, self.table_line.received, self.bench_line.received), (2, 1, 2))
        self.assertEqual((self.po1.status, self.po2.status), ('open', 'open'))
        self.assertEqual((self.chair.on_hand, self.table.on_hand, self.bench.on_hand), (2, 2, 2))
        self.assertEqual(self.session.status, 'received')
        self.assertIsNotNone(self.session.received_at)
        self.assertEqual(set(TruckScan.objects.filter(removed=False).values_list('voucher', flat=True)), {v1.pk, v2.pk})
        self.assertIsNone(TruckScan.objects.get(removed=True).voucher_id)

    def test_whole_po_received_closes_it(self):
        for piece in (1, 2, 3):
            truck.scan(self.session, self.code(self.bench_line, piece))
        truck.receive(self.session)
        self.po2.refresh_from_db()
        self.assertEqual(self.po2.status, 'closed')

    def test_failure_on_one_po_receives_nothing(self):
        truck.scan(self.session, self.code(self.chair_line, 1))
        truck.scan(self.session, self.code(self.bench_line, 1))
        self.bench.unorderable = True
        self.bench.save()
        with self.assertRaises(WorkflowError) as caught:
            truck.receive(self.session)
        self.assertIn(self.po2.number, str(caught.exception))
        self.assertIn('Nothing on this truck was received', str(caught.exception))
        self.session.refresh_from_db(); self.chair.refresh_from_db(); self.chair_line.refresh_from_db()
        self.assertEqual((self.session.status, self.chair.on_hand, self.chair_line.received), ('open', 0, 0))
        self.assertFalse(PurchaseDocument.objects.filter(kind='voucher').exists())

    def test_empty_truck_cannot_be_received(self):
        with self.assertRaises(WorkflowError):
            truck.receive(self.session)

    def test_reverse_voucher_frees_the_pieces(self):
        code = self.code(self.bench_line, 1)
        truck.scan(self.session, code)
        truck.scan(self.session, self.code(self.chair_line, 1))
        vouchers = {v.source_po_id: v for v in truck.receive(self.session)}
        voucher = vouchers[self.po2.pk]
        save_purchase(dict(token=voucher.token, version=voucher.version), [], 'voucher', 'cancel', self.po2)
        scan = TruckScan.objects.get(label__code=code)
        self.assertEqual((scan.removed, scan.removed_reason), (True, 'voucher reversed'))
        self.assertFalse(TruckScan.objects.get(label__line=self.chair_line).removed)   # the other PO's voucher stands
        self.bench_line.refresh_from_db()
        self.assertEqual(self.bench_line.received, 0)
        self.assertEqual(truck.scan(TruckSession.objects.create(), code)['kind'], 'ok')

    def test_cancel_truck_frees_everything(self):
        code = self.code(self.chair_line, 1)
        truck.scan(self.session, code)
        truck.cancel(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'cancelled')
        self.assertEqual(truck.scan(TruckSession.objects.create(), code)['kind'], 'ok')


@override_settings(MIDDLEWARE=AUTH)
class ScreenTests(TruckData):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.register = User.objects.create_user('register', password='x')
        self.register.groups.add(group(REGISTER_GROUP))
        self.admin = User.objects.create_user('morgan', password='x', first_name='Morgan')
        self.admin.groups.add(group(ADMIN_GROUP))
        self.stranger = User.objects.create_user('nobody', password='x')
        self.client.force_login(self.register)

    def post_json(self, url, data, client=None):
        return (client or self.client).post(url, json.dumps(data), content_type='application/json')

    def test_first_scan_starts_the_truck(self):
        page = self.client.get('/purchasing/truck/new/')
        self.assertContains(page, 'id="scan-box"')
        self.assertContains(page, 'autofocus')
        bad = self.post_json('/purchasing/truck/new/scan/', {'code': 'hello'}).json()
        self.assertEqual(bad['kind'], 'error')
        self.assertEqual(TruckSession.objects.count(), 1)   # no empty truck left behind (only setUp's)
        data = self.post_json('/purchasing/truck/new/scan/', {'code': self.code(self.chair_line, 1)}).json()
        self.assertEqual(data['kind'], 'ok')
        made = TruckSession.objects.get(token=data['token'])
        self.assertEqual(data['url'], f'/purchasing/truck/{made.token}/')
        self.assertEqual(made.created_by, self.register)
        self.assertIn('CHAIR-OAK', data['html'])
        self.assertEqual(data['totals'], dict(pieces=1, pos=1, vendors=1))

    def test_scan_screen_shows_the_truck_table(self):
        for piece in (1, 2):
            truck.scan(self.session, self.code(self.chair_line, piece))
        truck.scan(self.session, self.code(self.bench_line, 1))
        page = self.client.get(f'/purchasing/truck/{self.session.token}/').content.decode()
        for text in ('SAMPLE FURNITURE SUPPLY', 'DEMO OAK WORKS', self.po1.number, self.po2.number, 'CHAIR-OAK',
                     self.code(self.chair_line, 2), 'remove all on this line', 'Receive Items', self.session.number):
            self.assertIn(text, page)
        self.assertIn('<b id="t-pieces">3</b>', page)

    def test_scan_endpoint_kinds_and_two_laptops(self):
        laptop2 = self.client_class()
        laptop2.force_login(self.admin)
        base = f'/purchasing/truck/{self.session.token}/'
        first = self.post_json(base + 'scan/', {'code': self.code(self.chair_line, 1)}).json()
        self.assertEqual(first['kind'], 'ok')
        v = first['version']
        # Laptop 2 sees laptop 1's scan through /state/, and nothing new once it is up to date.
        state = laptop2.get(base + 'state/?v=0').json()
        self.assertEqual((state['version'], state['totals']['pieces']), (v, 1))
        self.assertEqual(laptop2.get(base + f'state/?v={v}').json(), dict(version=v, unchanged=True))
        second = self.post_json(base + 'scan/', {'code': self.code(self.bench_line, 1)}, laptop2).json()
        self.assertEqual(second['kind'], 'ok')
        state = self.client.get(base + f'state/?v={v}').json()
        self.assertEqual(state['totals'], dict(pieces=2, pos=2, vendors=2))
        self.assertIn('BENCH-MAPLE', state['html'])

        repeat = self.post_json(base + 'scan/', {'code': self.code(self.bench_line, 1)}).json()
        self.assertEqual(repeat['kind'], 'repeat')
        self.assertEqual(self.session.scans.get(label__line=self.bench_line).scanned_by, self.admin)


    def test_on_hand_column_shows_stock_before_the_truck_and_flags_zero(self):
        """an On Hand column (the Item List's figure) on the truck table and the last-scan panel, so a
        stock item at zero is seen while unloading; 0 or below is flagged. It is the stock before this truck."""
        base = f'/purchasing/truck/{self.session.token}/'
        first = self.post_json(base + 'scan/', {'code': self.code(self.chair_line, 1)}).json()   # chair on_hand 0
        self.assertEqual((first['on_hand_text'], first['on_hand_low']), ('0', True))
        second = self.post_json(base + 'scan/', {'code': self.code(self.table_line, 1)}).json()  # table on_hand 1
        self.assertEqual((second['on_hand_text'], second['on_hand_low']), ('1', False))
        html = self.client.get(base + 'state/?v=0').json()['html']
        self.assertIn('class="num on-hand low"', html)        # chair row
        self.assertIn('class="num on-hand"', html)            # table row
        page = self.client.get(base).content.decode()
        self.assertIn('<th class="num" title="Quantity on hand now, before this truck is received">On Hand</th>', page)
        self.assertIn('lr-onhand', page)
        # Still the stock before the truck until Receive Items posts the vouchers.
        self.chair.refresh_from_db()
        self.assertEqual(self.chair.on_hand, 0)

    def test_remove_endpoint(self):
        truck.scan(self.session, self.code(self.chair_line, 1))
        scan = self.session.scans.get()
        base = f'/purchasing/truck/{self.session.token}/'
        self.assertEqual(self.post_json(base + 'remove/', {'scan': scan.pk, 'reason': ''}).status_code, 400)
        data = self.post_json(base + 'remove/', {'scan': scan.pk, 'reason': 'damaged, going back'}).json()
        self.assertEqual((data['removed'], data['totals']['pieces']), (1, 0))
        self.assertIn('damaged, going back', data['html'])

    def test_receive_items_button(self):
        truck.scan(self.session, self.code(self.chair_line, 1))
        truck.scan(self.session, self.code(self.bench_line, 1))
        self.session.refresh_from_db()
        base = f'/purchasing/truck/{self.session.token}/'
        stale = self.client.post(base + 'receive/', {'version': self.session.version - 1})
        self.assertIn('error=', stale['Location'])
        self.assertFalse(PurchaseDocument.objects.filter(kind='voucher').exists())
        response = self.client.post(base + 'receive/', {'version': self.session.version})
        self.assertEqual(response['Location'], base + '?received=1')
        page = self.client.get(base + '?received=1').content.decode()
        vouchers = PurchaseDocument.objects.filter(kind='voucher', status='posted')
        self.assertEqual(vouchers.count(), 2)
        for v in vouchers:
            self.assertIn(v.number, page)
        self.assertNotIn('id="scan-box"', page)
        # Receiving History lists them; the PO page says which truck brought them in.
        history = self.client.get('/purchasing/history/').content.decode()
        for v in vouchers:
            self.assertIn(v.number, history)
        po_page = self.client.get(f'/purchasing/po/{self.po1.token}/').content.decode()
        self.assertIn('Received by truck', po_page)
        self.assertIn(self.session.number, po_page)

    def test_list_and_navigator_entry(self):
        truck.scan(self.session, self.code(self.chair_line, 1))
        page = self.client.get('/purchasing/truck/').content.decode()
        self.assertIn(self.session.number, page)
        self.assertIn('Continue scanning', page)
        self.assertIn('Start a Truck', page)
        home = self.client.get('/').content.decode()
        self.assertIn('href="/purchasing/truck/"', home)
        self.assertIn('Truck Receiving', home)
        held = self.client.get('/purchasing/held/').content.decode()
        self.assertIn('href="/purchasing/truck/"', held)

    def test_scan_refuses_get_and_strangers(self):
        base = f'/purchasing/truck/{self.session.token}/'
        self.assertEqual(self.client.get(base + 'scan/').status_code, 405)
        self.client.force_login(self.stranger)
        self.assertEqual(self.post_json(base + 'scan/', {'code': self.code(self.chair_line, 1)}).status_code, 403)
        self.assertEqual(self.client.get('/purchasing/truck/').status_code, 403)
        self.client.logout()
        self.assertEqual(self.post_json(base + 'scan/', {'code': self.code(self.chair_line, 1)}).status_code, 403)
        self.assertFalse(self.session.scans.exists())
