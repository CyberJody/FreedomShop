# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): Add Special Order Item on a sales order creates a real inventory item (Item List rules) and the
order gets an ordinary item line, so Generate PO fills in the item, vendor, description and cost."""
import json
import uuid
from decimal import Decimal as D

from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_register
from customers.models import Customer
from purchasing.models import Department, Vendor
from sales.models import Document, InventoryAdjustment, Item, Line
from sales.services import save_document


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class SpecialOrderItemTests(TestCase):
    def setUp(self):
        self.user = sign_in_register(self.client)   # the shared register login (non-admin) can use it
        self.vendor = Vendor.objects.create(code='KW', company='Demo Woodworks')
        self.other_dept = Department.objects.create(code='AAA', name='AAA FIRST', tax_rate=D('7'))
        self.spe = Department.objects.create(code='SPE', name='SPECIAL ORDER', tax_rate=D('7'))
        self.customer = Customer.objects.create(full_name='R65 Customer', customer_number='R65-1')

    def post(self, **body):
        data = dict(name='R65 SPECIAL TABLE', description='Oak table 42x60, special order', department=str(self.spe.pk),
                    vendor=str(self.vendor.pk), cost='450.00', price='899.95')
        data.update(body)
        return self.client.post('/pos/special-order-item/', json.dumps(data), content_type='application/json')

    def save_order(self, lines):
        token = str(uuid.uuid4())
        return save_document(dict(token=token, version=0, customer_id=self.customer.pk, lines=lines, action='save',
                                  notes=''), 'order', token)

    def test_order_page_offers_the_popup_with_spe_default_and_vendor_list(self):
        page = self.client.get('/pos/order/new/')
        self.assertContains(page, 'id="special-item-form"')
        self.assertContains(page, f'<option value="{self.spe.pk}" selected>SPE · SPECIAL ORDER</option>', html=False)
        self.assertContains(page, f'<option value="{self.vendor.pk}">Demo Woodworks (KW)</option>')
        self.assertNotContains(page, 'id="special-form"')   # the old free-text box is gone from orders
        # The sale screen keeps its Sell Miscellaneous Item box.
        sale = self.client.get('/pos/sale/new/')
        self.assertContains(sale, 'id="special-form"')
        self.assertNotContains(sale, 'id="special-item-form"')

    def test_save_and_add_creates_a_real_item_by_the_item_list_rules(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()['item']
        item = Item.objects.get(pk=result['id'])
        self.assertEqual((item.name, item.description, item.department, item.primary_vendor, item.vendor),
                         ('R65 SPECIAL TABLE', 'Oak table 42x60, special order', self.spe, self.vendor, 'KW'))
        self.assertEqual((item.order_cost, item.price, item.average_cost), (D('450.00'), D('899.95'), None))
        self.assertEqual(item.on_hand, 0)                      # a new item starts at 0
        self.assertEqual((item.tax_category, item.tax_rate), ('Taxable Sales', D('7')))
        self.assertTrue(item.sku and item.active and item.print_tags)
        self.assertEqual(item.version, 1)
        self.assertFalse(InventoryAdjustment.objects.filter(item=item).exists())
        self.assertEqual((result['label'], result['price'], result['vendor'], result['on_hand']),
                         ('R65 SPECIAL TABLE', '899.95', 'KW', '0'))

    def test_department_taxes_follow_the_department_like_the_item_form(self):
        non = Department.objects.create(code='NON', name='NON TAX DEPT', tax_category='Non', tax_rate=D('0'))
        item = Item.objects.get(pk=self.post(department=str(non.pk)).json()['item']['id'])
        self.assertEqual((item.tax_category, item.tax_rate), ('Non', D('0')))

    def test_vendor_and_price_and_name_are_required(self):
        for field, body in (('vendor', dict(vendor='')), ('price', dict(price='')), ('name', dict(name=''))):
            response = self.post(**body)
            self.assertEqual(response.status_code, 400, field)
            self.assertEqual(response.json()['field'], field)
        self.assertEqual(self.post(cost='12.345').status_code, 400)
        self.assertEqual(self.post(vendor='999999').json()['field'], 'vendor')
        self.vendor.active = False; self.vendor.save()
        self.assertEqual(self.post().status_code, 400)        # inactive vendors aren't offered or accepted
        self.assertFalse(Item.objects.exists())

    def test_same_name_asks_first_like_r60(self):
        Item.objects.create(sku='6045', name='R65 SPECIAL TABLE', on_hand=0)
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['duplicate'], 'Item #6045 already uses this name. Save anyway?')
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(self.post(confirm_duplicate_name=True).status_code, 200)
        self.assertEqual(Item.objects.count(), 2)

    def test_generate_po_fills_in_item_vendor_description_and_cost(self):
        item = Item.objects.get(pk=self.post().json()['item']['id'])
        order = self.save_order([dict(
            item_id=item.pk, sku=item.sku, item_name=item.name, description=item.description, quantity='2',
            price='899.95', original_price='899.95', tax_rate='7', vendor=item.vendor)])
        page = self.client.get(f'/purchasing/po/new/?order={order.token}')
        self.assertEqual(page.status_code, 200)
        form = page.context['formset'].forms[0]
        self.assertEqual((form.initial['item'], form.initial['item_description'], form.initial['cost'], form.initial['quantity']),
                         (item.pk, 'Oak table 42x60, special order', D('450.00'), D('2')))
        self.assertEqual(page.context['header'].initial['vendor'], self.vendor.pk)

    def post_page(self, url, action):
        """Post a purchasing editor page exactly as it was offered (header + every line's initial values)."""
        page = self.client.get(url)
        header, formset = page.context['header'], page.context['formset']
        data = {k: ('' if v is None else v) for k, v in header.initial.items()}
        data.update({f'lines-{k}': v for k, v in formset.management_form.initial.items()})
        for i, form in enumerate(formset.forms):
            data.update({f'lines-{i}-{k}': ('' if v is None else v) for k, v in form.initial.items()})
        data['action'] = action
        return self.client.post(url.split('?')[0] if 'new' not in url else url, data)

    def test_the_po_from_the_order_can_be_received_and_the_order_picked_up(self):
        """End to end: special item -> order -> Generate PO -> save -> Receive Items -> pickup. Receiving a PO made by
        Generate PO used to be refused ("Select a matching item from an active customer order.") because the voucher
        line was offered with the order link that only PO lines may carry."""
        from purchasing.models import PurchaseDocument
        from sales.services import fulfill_order
        item = Item.objects.get(pk=self.post().json()['item']['id'])
        order = self.save_order([dict(item_id=item.pk, sku=item.sku, item_name=item.name, description=item.description,
                                      quantity='2', price='899.95', original_price='899.95', tax_rate='7', vendor=item.vendor)])
        response = self.post_page(f'/purchasing/po/new/?order={order.token}', 'save')
        self.assertEqual(response.status_code, 302, response.context and response.context.get('error'))
        po = PurchaseDocument.objects.get(kind='po')
        self.assertEqual((po.vendor, po.lines.get().sales_line.document, po.lines.get().cost), (self.vendor, order, D('450.00')))
        response = self.post_page(f'/purchasing/voucher/new/?po={po.token}', 'post')
        self.assertEqual(response.status_code, 302, response.context and response.context.get('error'))
        item.refresh_from_db()
        self.assertEqual((item.on_hand, item.average_cost), (2, D('450.00')))
        po.refresh_from_db()
        self.assertEqual(po.lines.get().due, 0)
        order.refresh_from_db()
        from sales.services import save_order_payment
        save_order_payment(order.pk, {'version': order.version, 'token': str(uuid.uuid4()), 'amount': str(order.total),
                                      'method': 'credit'})
        order.refresh_from_db()
        receipt = fulfill_order(order.pk, {'version': order.version, 'token': str(uuid.uuid4()),
                                           f'qty_{order.lines.get().pk}': '2'})
        item.refresh_from_db()
        self.assertEqual((receipt.lines.get().item, item.on_hand), (item, 0))

    def test_old_free_text_special_order_lines_still_save_and_show(self):
        order = self.save_order([dict(
            item_id=None, sku='OLD', item_name='OLD FREE TEXT', description='Imported-style special line', quantity='1',
            price='10.00', tax_rate='7', vendor='Someone')])
        line = Line.objects.get(document=order)
        self.assertIsNone(line.item_id)
        self.assertContains(self.client.get(f'/pos/order/{order.token}/'), 'Imported-style special line')

    def test_get_is_refused(self):
        self.assertEqual(self.client.get('/pos/special-order-item/').status_code, 405)
