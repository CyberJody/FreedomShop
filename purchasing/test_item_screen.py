# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from PIL import Image

from sales.models import Item, Line, Document
from sales.services import save_document, WorkflowError
from .item_views import activity
from .models import ItemVendor, Vendor
from .services import save_purchase
from .tests import header, rows


class ItemScreenTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='PRIMARY', company='Primary Vendor')
        self.alternate = Vendor.objects.create(code='ALT', company='Alternate Vendor')
        self.data = dict(name='Cabinet & Hutch', sku='0015063', item_type='inventory',
            description='Detailed cabinet description', price='1199.95', on_hand='2',
            average_cost='400', order_cost='500', tax_category='Taxable Sales', tax_rate='7',
            upc='001234500001', alu='CAB-OAK', wood='Oak', size='Large', style='CABINET',
            primary_vendor=self.vendor.pk, unit_of_measure='Each', manufacturer='Example Maker',
            location='Floor 2', oil_su='Oiled', reverse_notavail='See reverse', pull='Brass', finish='Finished',
            comments='Handle carefully <script>not code</script>', quick_pick_group='Cabinets',
            reorder_point='1', price_2='1100', price_3='1000', price_4='', price_5='0',
            print_tags='on', shipping_weight='50', shipping_weight_unit='lb',
            shipping_height='70', shipping_length='40', shipping_width='20', shipping_dimension_unit='in',
            active='on', version='1')

    def vendors(self, vendor=None, **changes):
        data = {'vendors-TOTAL_FORMS':'1', 'vendors-INITIAL_FORMS':'0',
                'vendors-0-vendor': (vendor or self.alternate).pk,
                'vendors-0-order_cost':'450', 'vendors-0-upc':'000ALTUPC', 'vendors-0-alu':'ALT-CAB'}
        data.update(changes)
        return data

    def create(self, **changes):
        data = {**self.data, **changes}
        response = self.client.post('/purchasing/items/new/', data)
        self.assertEqual(response.status_code, 302, str(response.context['form'].errors) if response.context else '')
        return Item.objects.latest('pk')

    def test_separate_fields_save_next_reopen_and_lookup(self):
        item = self.create(**self.vendors(), action='save-next')
        for field in ['name', 'upc', 'alu', 'wood', 'size', 'style', 'manufacturer', 'location',
                      'oil_su', 'reverse_notavail', 'pull', 'finish', 'comments', 'tax_category', 'unit_of_measure']:
            self.assertEqual(getattr(item, field), self.data[field])
        self.assertEqual(item.price_5, 0)
        self.assertEqual(item.shipping_weight, 50)
        self.assertEqual(item.alternate_vendors.get().upc, '000ALTUPC')
        response = self.client.get(f'/purchasing/items/{item.pk}/')
        self.assertContains(response, '&lt;script&gt;not code&lt;/script&gt;')
        self.assertNotContains(response, 'Mobile Sync')
        self.assertNotContains(response, 'QuickBooks Options')
        for query in ['cabinet hutch', item.sku, '001234500001', 'CAB-OAK', '000ALTUPC', 'ALT-CAB']:
            for endpoint in ['/pos/lookup/items/', '/purchasing/lookup/items/']:
                with self.subTest(query=query, endpoint=endpoint):
                    result = self.client.get(endpoint, {'q':query}).json()['results']
                    self.assertEqual([r['id'] for r in result], [item.pk])
        self.assertContains(self.client.get('/purchasing/items/', {'q':'cabinet hutch'}), f'data-catalog-id="{item.pk}"')
        self.assertTrue(item.print_tags)
        alternate_cost = self.client.get('/purchasing/lookup/items/', {'q':item.name, 'vendor':self.alternate.pk}).json()['results'][0]['cost']
        self.assertEqual(Decimal(alternate_cost), Decimal('450'))
        self.assertEqual(self.client.post(f'/purchasing/items/{item.pk}/', self.data).status_code,302)
        self.assertEqual(self.client.post(f'/purchasing/items/{item.pk}/', self.data).status_code,400)
        self.assertEqual(item.alternate_vendors.count(),1)

    def test_invalid_alternate_vendor_rolls_back_parent_and_shipping_validation(self):
        response = self.client.post('/purchasing/items/new/', {**self.data, **self.vendors(self.vendor)})
        self.assertEqual(response.status_code,400)
        self.assertFalse(Item.objects.exists())
        for change in [{'shipping_weight_unit':''}, {'shipping_dimension_unit':''}, {'price_2':'-1'},
                       {'shipping_height':'-1'}, {'reorder_point':'-1'}, {'name':''}]:
            response = self.client.post('/purchasing/items/new/', {**self.data, **change})
            self.assertEqual(response.status_code,400)
            self.assertFalse(Item.objects.exists())

    def test_save_next_starts_a_blank_new_item(self):
        response = self.client.post('/purchasing/items/new/', {**self.data, 'action':'save-next'})
        self.assertRedirects(response, '/purchasing/items/new/?saved=1')
        page = self.client.get(response.url)
        self.assertContains(page, 'Ready for the next item')
        self.assertEqual(page.context['form']['name'].value(), '')

    def test_save_preserves_list_position(self):
        from urllib.parse import parse_qs, urlsplit
        target = '/purchasing/items/?source=inventory&q=Cabinet&at=120&page=3'
        response = self.client.post('/purchasing/items/new/', {**self.data, 'return_to':target})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response.url).query)['return_to'], [target])
        self.assertEqual(self.client.get(response.url).context['return_to'], target)

    def test_edit_changes_stock_cost_and_stale_vendor_changes_are_rejected(self):
        item = self.create(**self.vendors())
        alternate = item.alternate_vendors.get()
        data = {**self.data, **self.vendors(), 'vendors-INITIAL_FORMS':'1', 'vendors-0-id':alternate.pk,
                'on_hand':'999', 'average_cost':'999', 'vendors-0-order_cost':'455'}
        self.assertEqual(self.client.post(f'/purchasing/items/{item.pk}/', data).status_code,302)
        item.refresh_from_db(); alternate.refresh_from_db()
        self.assertEqual((item.on_hand,item.average_cost,alternate.order_cost),(999,999,455))
        data['vendors-0-order_cost']='1'
        self.assertEqual(self.client.post(f'/purchasing/items/{item.pk}/', data).status_code,400)
        alternate.refresh_from_db(); self.assertEqual(alternate.order_cost,455)

    def test_photo_validation_storage_and_removal(self):
        image = BytesIO(); Image.new('RGB',(16,16),'red').save(image,format='PNG')
        item = self.create(photo_upload=SimpleUploadedFile('photo.png',image.getvalue(),content_type='image/png'))
        response = self.client.get(f'/purchasing/items/{item.pk}/photo/')
        self.assertEqual(response['Content-Type'],'image/jpeg')
        self.assertTrue(response.content.startswith(b'\xff\xd8'))
        self.assertEqual(self.client.post(f'/purchasing/items/{item.pk}/', {**self.data, 'remove_photo':'on'}).status_code,302)
        self.assertEqual(self.client.get(f'/purchasing/items/{item.pk}/photo/').status_code,404)
        response = self.client.post('/purchasing/items/new/', {**self.data, 'sku':'BAD',
            'photo_upload':SimpleUploadedFile('bad.png',b'<script>bad</script>',content_type='image/png')})
        self.assertEqual(response.status_code,400)
        self.assertFalse(Item.objects.filter(sku='BAD').exists())

    def test_copy_preserves_attributes_but_clears_independent_identifiers(self):
        item = self.create(**self.vendors())
        response = self.client.get('/purchasing/items/new/', {'copy':item.pk})
        form = response.context['form']
        self.assertEqual(form['name'].value(),item.name)
        self.assertEqual(form['wood'].value(),'Oak')
        self.assertFalse(form['sku'].value())
        for field in ['upc','alu','average_cost','order_cost']:
            self.assertEqual(form[field].value(),getattr(item,field))
        self.assertEqual(form['on_hand'].value(),0)
        self.assertEqual(len(response.context['vendors'].initial_forms),0)
        self.assertEqual(response.context['vendors'].forms[0]['vendor'].value(),self.alternate.pk)

    def test_unorderable_is_enforced_and_serial_flag_is_reference_only(self):
        item = self.create(unorderable='on')
        with self.assertRaisesMessage(WorkflowError,'Unorderable'):
            save_purchase(header(self.vendor),rows(item),'po','save')
        item.unorderable=False; item.use_serial_numbers=True; item.save()
        save_purchase(header(self.vendor),rows(item),'voucher','post')
        from customers.models import Customer
        customer=Customer.objects.create(source_id='serial-test',full_name='Test')
        save_document(dict(action='complete',customer_id=customer.pk,lines=[dict(item_id=item.pk,quantity='1',price='1')],payment_method='cash',tendered='10'),'sale',uuid.uuid4())
        item.refresh_from_db(); self.assertEqual(item.on_hand,3)

    def test_history_profit_and_names_use_transaction_snapshots(self):
        item = self.create()
        from customers.models import Customer
        customer=Customer.objects.create(source_id='history-test',full_name='Test')
        doc = save_document(dict(action='complete',customer_id=customer.pk,lines=[dict(item_id=item.pk,quantity='1',price='1000')],
                            payment_method='cash',tendered='1070'),'sale',uuid.uuid4())
        item.name='Renamed'; item.average_cost=900; item.save()
        sale = doc.lines.get()
        self.assertEqual(sale.item_name,'Cabinet & Hutch')
        self.assertEqual(sale.sku,item.sku)
        self.assertEqual(sale.unit_cost,400)
        stats = activity(item)
        self.assertEqual((stats['cogs'],stats['profit'],stats['sold']),(400,600,1))
        self.assertEqual(stats['history'][0]['number'],doc.number)
        sale.unit_cost=None; sale.save()
        self.assertIsNone(activity(item)['profit'])

    @override_settings(MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'])
    def test_item_writes_require_csrf(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post('/purchasing/items/new/', self.data).status_code,403)

    def test_price_tag_obeys_print_flag(self):
        item=self.create()
        self.assertContains(self.client.get(f'/purchasing/items/{item.pk}/tag/'),'001234500001')
        item.print_tags=False;item.save()
        self.assertContains(self.client.get(f'/purchasing/items/{item.pk}/tag/'),'Print Tags is off')


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class ItemTagPrintTests(TestCase):
    def setUp(self):
        from sales.models import Printer
        self.item = Item.objects.create(name='TEST Cabinet', sku='123', price='1234.56', print_tags=True)
        self.label = Printer.objects.create(name='Zebra Tag Printer', kind='label', host='127.0.0.1')
        self.letter = Printer.objects.create(name='Brother', kind='letter', host='test.invalid')
        self.url = f'/purchasing/items/{self.item.pk}/tag/print/'

    def test_picker_order_and_disabled_flag(self):
        page = self.client.get(f'/purchasing/items/{self.item.pk}/tag/')
        self.assertContains(page, 'Print Tag')
        self.assertContains(page, 'tagPrinter')
        self.assertLess(page.content.index(b'Zebra Tag Printer'), page.content.index(b'>Brother<'))
        self.item.print_tags = False
        self.item.save()
        page = self.client.get(f'/purchasing/items/{self.item.pk}/tag/')
        self.assertNotContains(page, 'id="tag-print"')
        self.assertEqual(self.client.post(self.url, {'printer': self.label.pk}).status_code, 400)

    def test_invalid_printer_copies_and_method_never_send(self):
        from unittest import mock
        from sales.models import Printer
        inactive = Printer.objects.create(name='Off', kind='label', active=False, host='test.invalid')
        receipt = Printer.objects.create(name='Star', kind='receipt', host='test.invalid')
        with mock.patch('sales.printing.socket.create_connection') as connect:
            for key in ['unknown', '999999999999999999999999999999', '99999', inactive.pk, receipt.pk]:
                self.assertEqual(self.client.post(self.url, {'printer': key}).status_code, 400)
            for copies in ['0', '11', '-1', '1.5', 'bad', '999999999999999999999999999999999999']:
                self.assertEqual(self.client.post(self.url, {'printer': self.label.pk, 'copies': copies}).status_code, 400)
            self.assertEqual(self.client.get(self.url).status_code, 405)
            connect.assert_not_called()

    def test_label_post_uses_fake_socket_and_remembers_printer(self):
        from unittest import mock
        with mock.patch('sales.printing.socket.create_connection') as connect:
            conn = connect.return_value.__enter__.return_value
            conn.recv.return_value = b''
            response = self.client.post(self.url, {'printer': self.label.pk, 'copies': '2'})
            self.assertEqual(response.json(), {'message': 'Sent 2 tags to Zebra Tag Printer.'})
            conn.sendall.assert_called_once()
            self.assertIn(b'^PQ2', conn.sendall.call_args.args[0])
        self.assertEqual(self.client.session['label_printer'], str(self.label.pk))  # Zebra remembered on its own
        self.assertContains(self.client.get(f'/purchasing/items/{self.item.pk}/tag/'),
                            f'value="{self.label.pk}" selected')

    def test_letter_path_sends_all_copies_as_sheets(self):
        from unittest import mock
        with mock.patch('reports.direct_print.send_letter') as send_letter:
            response = self.client.post(self.url, {'printer': self.letter.pk, 'copies': '3'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(send_letter.call_args.args[1]), 3)
        self.assertEqual(self.client.session['tag_printer'], str(self.letter.pk))

    def test_failure_returns_502_and_does_not_remember(self):
        from unittest import mock
        with mock.patch('sales.printing.socket.create_connection', side_effect=OSError):
            response = self.client.post(self.url, {'printer': self.label.pk})
        self.assertEqual(response.status_code, 502)
        self.assertIn('Could not reach', response.json()['error'])
        self.assertNotIn('tag_printer', self.client.session)

    @override_settings(MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'])
    def test_print_requires_csrf(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post(self.url,
            {'printer': self.label.pk}).status_code, 403)
