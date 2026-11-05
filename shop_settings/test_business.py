# SPDX-License-Identifier: AGPL-3.0-or-later
"""Business Settings: the screen (admin only), a new shop's placeholder row, the logo upload, the test receipt (rendered
for the screen, never printed) and the saved values reaching receipts, orders, POs, report exports and page titles.
TEST data only."""
import io
import json
import uuid
from decimal import Decimal as D
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register
from . import config
from .models import Business
from .test_print_parity import GOLDEN, PrintParityTests
from .tests import FULL_MIDDLEWARE, SettingsCase


def png(size=(400, 200)):
    image = Image.new('L', size, 255)
    image.paste(0, (20, 20, size[0] - 20, size[1] - 20))
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def form_data(row, **changes):
    data = {name: getattr(row, name) for name in ('name', 'legal_name', 'short_code', 'address_line1', 'address_line2', 'city', 'region',
                                                  'postal_code', 'country', 'phone', 'fax', 'email', 'website', 'tagline', 'receipt_header_lines',
                                                  'return_policy', 'receipt_footer', 'order_return_policy', 'order_address_lines',
                                                  'receipt_copies', 'order_copies_default', 'label_url', 'serial_prefix')}
    data['version'] = row.version
    data.update(changes)
    return data


class PlaceholderTests(SettingsCase):
    def test_a_new_shop_starts_as_my_shop(self):
        Business.objects.all().delete()
        with override_settings(SHOP_SETTINGS_SEED=''):
            config.clear_cache()
            shop = config.business()
        self.assertEqual((shop.name, shop.legal_name, shop.address_line1, bool(shop.logo), shop.receipt_copies, shop.order_copies_default),
                         ('My Shop', '', '', False, 2, 3))
        self.assertIn('Business Settings', shop.return_policy)
        self.assertEqual(shop.receipt_lines(), ['My Shop'])
        self.assertEqual(Business.objects.count(), 1)   # the placeholder row is saved on first use

    def test_name_prints_once_without_a_logo_or_legal_name(self):
        from sales.printing import Receipt, render_sample_receipt
        row = config.business()
        row.name, row.legal_name, row.receipt_header_lines, row.address_line1 = 'Corner Shop', '', '', '5 Elm St'
        row.save()
        drawn = []
        original = Receipt.text

        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        with override_settings(PRINT_LOGO_FILE=''), mock.patch.object(Receipt, 'text', spy):
            render_sample_receipt()
        self.assertEqual((drawn.count('Corner Shop'), drawn.count('5 Elm St')), (1, 1))

    def test_lines_are_built_from_the_address_when_not_given(self):
        shop = Business(name='Corner Shop', legal_name='Corner Shop LLC', address_line1='5 Elm St', city='Springfield', region='IL',
                        postal_code='62701', phone='217-555-0100')
        self.assertEqual(shop.receipt_lines(), ['Corner Shop LLC', '5 Elm St', 'Springfield, IL 62701', '217-555-0100'])
        self.assertEqual(shop.order_lines(), ['Corner Shop LLC', '5 Elm St - Springfield, IL 62701', '217-555-0100'])
        self.assertEqual(shop.address_lines(), ['5 Elm St', 'Springfield, IL 62701'])
        shop.return_policy = 'No returns.'
        self.assertEqual(shop.order_policy, 'No returns.')


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class BusinessScreenTests(SettingsCase):
    def test_admin_only(self):
        sign_in_register(self.client)
        for url in ('/settings/business/', '/settings/business/test-receipt.png', '/settings/business/logo/'):
            self.assertRedirects(self.client.get(url), '/?admin=off', fetch_redirect_response=False)
        self.assertEqual(self.client.post('/settings/business/', {}).status_code, 403)
        sign_in_admin(self.client)
        self.assertContains(self.client.get('/settings/business/'), 'Print test receipt')
        self.assertIn({'label': 'Business Settings', 'url': '/settings/business/', 'icon': 'reports'}, self.client.get('/').context['report_tasks'])

    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=True)
    def test_signed_out_goes_to_sign_in(self):
        response = self.client.get('/settings/business/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_save_keeps_business_identity_separate_from_product_chrome(self):
        sign_in_admin(self.client)
        row = config.business()
        response = self.client.post('/settings/business/', form_data(row, name='Corner Shop', legal_name='Corner Shop LLC'))
        self.assertRedirects(response, '/settings/business/?saved=1', fetch_redirect_response=False)
        self.assertEqual((Business.objects.get().name, Business.objects.get().version), ('Corner Shop', row.version + 1))
        home = self.client.get('/')
        self.assertContains(home, '<title>FreedomShop · Point of Sale</title>')
        self.assertContains(home, 'Corner Shop')
        self.assertContains(home, 'https://github.com/CyberJody/FreedomShop/issues')
        customers = self.client.get('/customers/')
        self.assertContains(customers, 'FreedomShop <span class="product-subtitle">Point of Sale</span>')
        # The product name leads the header; the shop's name sits on its own smaller line under it.
        for page in (home, customers, self.client.get('/pos/sale/new/')):
            self.assertContains(page, '<span class="product-subtitle">Point of Sale</span></span>'
                                      '<span class="business-name">Corner Shop</span>')
        self.client.logout()
        login = self.client.get('/login/')
        self.assertContains(login, 'Welcome to FreedomShop')
        self.assertContains(login, 'Corner Shop')
        sign_in_admin(self.client)
        stale = self.client.post('/settings/business/', form_data(row, name='Other'))
        self.assertContains(stale, 'Someone changed the business settings', status_code=400)
        bad = self.client.post('/settings/business/', form_data(Business.objects.get(), label_url='shop.example.com/?wc={code}'))
        self.assertContains(bad, 'starting with https://', status_code=400)

    def test_logo_upload_preview_print_and_remove(self):
        from sales.printing import logo_path
        sign_in_admin(self.client)
        row = config.business()
        upload = SimpleUploadedFile('logo.png', png(), content_type='image/png')
        self.assertEqual(self.client.post('/settings/business/', {**form_data(row), 'logo': upload}).status_code, 302)
        row = Business.objects.get()
        self.assertTrue(row.logo.name.startswith('branding/'))
        self.assertEqual(logo_path(), __import__('pathlib').Path(row.logo.path))
        preview = self.client.get('/settings/business/logo/')
        self.assertEqual((preview.status_code, preview['Content-Type']), (200, 'image/png'))
        self.assertEqual(b''.join(preview.streaming_content), png())
        self.assertContains(self.client.get('/settings/business/'), 'alt="Current logo"')
        not_image = SimpleUploadedFile('logo.png', b'not an image', content_type='image/png')
        self.assertEqual(self.client.post('/settings/business/', {**form_data(row), 'logo': not_image}).status_code, 400)
        self.assertEqual(self.client.post('/settings/business/', {**form_data(row), 'remove_logo': 'on'}).status_code, 302)
        self.assertFalse(Business.objects.get().logo)
        self.assertEqual(self.client.get('/settings/business/logo/').status_code, 404)

    @mock.patch('sales.printing.socket.create_connection')
    def test_test_receipt_renders_and_never_prints(self, connect):
        sign_in_admin(self.client)
        row = config.business()
        self.client.post('/settings/business/', form_data(row, name='Corner Shop', receipt_header_lines='Corner Shop LLC\n5 Elm St', return_policy='No returns.'))
        from sales.printing import Receipt
        drawn = []
        original = Receipt.text

        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        with mock.patch.object(Receipt, 'text', spy):
            response = self.client.get('/settings/business/test-receipt.png')
        self.assertEqual((response.status_code, response['Content-Type']), (200, 'image/png'))
        self.assertEqual(Image.open(io.BytesIO(response.content)).width, 576)
        for text in ('Corner Shop LLC', '5 Elm St', 'No returns.'):
            self.assertIn(text, drawn)
        connect.assert_not_called()


class BusinessValuesReachPrintoutsTests(SettingsCase):
    def setUp(self):
        super().setUp()
        row = config.business()
        for name, value in dict(name='Corner Shop', legal_name='Corner Shop LLC', short_code='CS1', address_line1='5 Elm St', city='Springfield',
                                region='IL', postal_code='62701', phone='217-555-0100', fax='217-555-0101', tagline='', receipt_header_lines='',
                                return_policy='Returns within 30 days.', order_return_policy='', order_address_lines='', receipt_footer='Thanks!',
                                receipt_copies=1, label_url='https://shop.example.com/i/{code}').items():
            setattr(row, name, value)
        row.save()
        from customers.models import Customer
        from sales.models import Item
        self.customer = Customer.objects.create(source_id='TEST-biz', full_name='TEST Buyer')
        self.item = Item.objects.create(sku='TEST-B1', name='TEST Lamp', description='Lamp', price=D('50.00'), on_hand=5, tax_rate=7)

    def test_receipt_and_copies(self):
        from sales.printing import Receipt, receipt_copies, render_receipt
        from sales.services import save_document
        sale = save_document(dict(action='complete', customer_id=self.customer.pk, lines=[dict(item_id=self.item.pk, quantity='1', price='50.00', tax_rate='7')],
                                  payments=[dict(method='cash', amount='60.00')]), 'sale', uuid.uuid4())
        drawn = []
        original = Receipt.text

        def spy(self, text, *args, **kwargs):
            drawn.append(text)
            return original(self, text, *args, **kwargs)
        with mock.patch.object(Receipt, 'text', spy):
            render_receipt(sale)
        for text in ('Corner Shop LLC', '5 Elm St', 'Springfield, IL 62701', '217-555-0100', 'Returns within 30 days.'):
            self.assertIn(text, drawn)
        self.assertFalse(any('Demo Home & Hardware' in text or '123 Main St' in text for text in drawn))
        self.assertEqual(receipt_copies(sale), 1)

    def test_sales_order_po_reports_and_labels(self):
        from purchasing.models import Vendor
        from purchasing.piece_codes import piece_url
        from purchasing.print_exports import pdf_bytes
        from purchasing.services import save_purchase
        from purchasing.tests import header, rows
        from reports import inventory_exports, inventory_on_hand
        from sales.letter_printing import Pages, render_sales_order
        from sales.services import save_document
        import pypdfium2 as pdfium
        from openpyxl import load_workbook
        order = save_document(dict(action='save', customer_id=self.customer.pk, lines=[dict(item_id=self.item.pk, quantity='1', price='50.00', tax_rate='7')]),
                              'order', uuid.uuid4())
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(order)
        texts = [step[3] for steps in pages for step in steps if step[0] == 'text']
        for text in ('Store: CS1', 'Thanks!', 'Returns within 30 days.', 'Corner Shop LLC', '5 Elm St - Springfield, IL 62701', '217-555-0100'):
            self.assertIn(text, texts)
        vendor = Vendor.objects.create(code='TV', company='TEST Vendor')
        po = save_purchase(header(vendor), rows(self.item, '1', '20'), 'po', 'save')
        pdf = pdfium.PdfDocument(pdf_bytes(po))
        text = pdf[0].get_textpage().get_text_range()
        self.assertEqual(pdf.get_metadata_dict()['Author'], 'Corner Shop LLC')
        pdf.close()
        for part in ('Corner Shop LLC', 'Springfield, IL 62701', 'Phone 217-555-0100', 'Fax 217-555-0101'):
            self.assertIn(part, text)
        book = load_workbook(io.BytesIO(inventory_exports.xlsx_bytes(inventory_on_hand.build())))
        self.assertEqual(book.worksheets[0]['A1'].value, 'Corner Shop')
        with override_settings(PIECE_LABEL_URL='https://example.com/?wc={code}'):
            self.assertEqual(piece_url('WC-ABCD-EFG2'), 'https://shop.example.com/i/WC-ABCD-EFG2')   # Business Settings wins

    def test_parity_check_notices_a_different_business(self):
        golden = json.loads(GOLDEN.read_text())
        case = PrintParityTests('test_printouts_match_the_recording')
        case.setUp()
        try:
            out = case.outputs()
        finally:
            case.doCleanups()
        changed = [name for name in golden if out[name] != golden[name]]
        for name in ('receipt', 'sales_order', 'po_pdf', 'sales_tax_pdf', 'inventory_xlsx', 'letter_tag', 'set_tag', 'receipt_copies'):
            self.assertIn(name, changed)
