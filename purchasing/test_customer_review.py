# SPDX-License-Identifier: AGPL-3.0-or-later
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from django.db import connection, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from sales.models import Item
from .catalog_quantities import catalog_quantities
from .forms import ItemForm
from .item_numbers import next_item_number
from .models import Vendor, ItemVendor, PurchaseDocument
from .services import save_purchase
from .tests import header, rows


class CustomerReviewTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='MAIN', company='Main Vendor')
        self.item = Item.objects.create(sku='16962', name='ANR-BEAVER', description='BEAVER TAXIDERMY',
            price='1199.95', average_cost='601.234567', order_cost='701.345678', on_hand=1,
            primary_vendor=self.vendor, tax_rate=7, tax_category='Taxable Sales', comments='Original comments',
            shipping_weight=0, shipping_height=0, shipping_length=0, shipping_width=0)

    def test_copy_modal_saves_new_identity_and_retains_details_without_copying_stock(self):
        alternate = Vendor.objects.create(code='ALT', company='Alternate Vendor')
        ItemVendor.objects.create(item=self.item, vendor=alternate, order_cost='650.123456', upc='ALT-UPC')
        url=f'/purchasing/items/new/?copy={self.item.pk}&modal=1'
        page=self.client.get(url)
        form=page.context['form']
        self.assertEqual(page['X-Frame-Options'],'SAMEORIGIN')
        self.assertEqual(form['name'].value(), 'ANR-BEAVER')
        self.assertEqual(form['on_hand'].value(),0)
        self.assertEqual(form['order_cost'].value(),Decimal('701.35'))  # whole cents, half-up
        self.assertTrue(form.fields['sku'].disabled)
        self.assertContains(page,'Save &amp; New')
        data={field: form[field].value() for field in form.fields if form[field].value() is not None}
        for field in ['photo_upload','photo','remove_photo']:data.pop(field,None)
        data.update(name='ANR-BEAVER-COPY',sku='ATTEMPTED-OVERRIDE',
            **{'vendors-TOTAL_FORMS':'1','vendors-INITIAL_FORMS':'0','vendors-0-vendor':alternate.pk,
               'vendors-0-order_cost':'650.123456','vendors-0-upc':'ALT-UPC'})
        rejected=self.client.post(url,data)  # costs and prices take at most 2 decimal places
        self.assertEqual(rejected.status_code,400)
        self.assertIn('2 decimal places', str(rejected.context['vendor_formset'].errors if 'vendor_formset' in rejected.context else rejected.content))
        data['vendors-0-order_cost']='650.13'
        response=self.client.post(url,data)
        self.assertEqual(response.status_code,302, response.context['form'].errors if response.context else '')
        copied=Item.objects.exclude(pk=self.item.pk).get()
        self.assertEqual(copied.sku,'16963');self.assertEqual(copied.on_hand,0)
        self.assertEqual(copied.order_cost,Decimal('701.35'))
        self.assertEqual(copied.comments,self.item.comments)
        self.assertEqual(copied.alternate_vendors.get().order_cost,Decimal('650.13'))
        self.assertIn('modal=1',response.url)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,1);self.assertEqual(self.item.name,'ANR-BEAVER')
        self.assertIsNone(copied.imported_on_order);self.assertIsNone(copied.source_id)

    def test_form_save_also_allocates_a_number_and_edit_preserves_it(self):
        data=dict(name='New',item_type='inventory',price='4',on_hand='-1',tax_rate='7',active='on')
        form=ItemForm(data);self.assertTrue(form.is_valid(),form.errors)
        item=form.save();self.assertEqual(item.sku,'16963');self.assertEqual(item.on_hand,-1)
        form=ItemForm({**data,'sku':'tampered'},instance=item);self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().sku,'16963')

    def test_picker_search_prices_vendor_cost_and_inactive_filter(self):
        alt=Vendor.objects.create(code='ALT',company='Alternative')
        ItemVendor.objects.create(item=self.item,vendor=alt,order_cost='650.125000')
        Item.objects.create(sku='OFF',name='ANR-BEAVER-OFF',active=False)
        data=self.client.get('/purchasing/item-picker/',{'q':'beaver taxidermy','vendor':alt.pk}).json()
        self.assertEqual(data['count'],1)
        self.assertEqual(data['results'][0]['price'],'1199.95')
        self.assertEqual(data['results'][0]['cost'],'650.13')  # 650.125 rounded half-up
        self.assertEqual(data['results'][0]['label'],'ANR-BEAVER')
        self.assertEqual(self.client.get('/purchasing/item-picker/?vendor=bad').status_code,400)
        self.assertEqual(self.client.post('/purchasing/item-picker/').status_code,405)

    def test_imported_and_new_receiving_dates_and_open_order_quantities(self):
        earlier=(timezone.now()-timezone.timedelta(days=60)).date()
        Item.objects.filter(pk=self.item.pk).update(imported_on_order=3,imported_last_received=earlier)
        value=catalog_quantities(Item.objects.all()).get(pk=self.item.pk)
        self.assertEqual(value.on_order,0);self.assertEqual(value.last_received,earlier)  # QB's item total is not read; only PO lines count
        po=save_purchase(header(self.vendor),rows(self.item,'4'),'po','save')
        value=catalog_quantities(Item.objects.all()).get(pk=self.item.pk);self.assertEqual(value.on_order,4)
        voucher=save_purchase(header(self.vendor),rows(self.item,'1',source_line=po.lines.get().pk),'voucher','post',po)
        value=catalog_quantities(Item.objects.all()).get(pk=self.item.pk)
        self.assertEqual(value.on_order,3);self.assertEqual(value.last_received,timezone.localtime(voucher.posted_at).date())
        # Cancellation removes the app receipt from the derived date.
        save_purchase(header(self.vendor,token=voucher.token,version=voucher.version),[],'voucher','cancel',po)
        value=catalog_quantities(Item.objects.all()).get(pk=self.item.pk)
        self.assertEqual(value.last_received,earlier);self.assertEqual(value.on_order,4)

    def test_receiving_can_correct_negative_stock_without_negative_average_cost(self):
        self.item.on_hand=-3;self.item.save()
        save_purchase(header(self.vendor),rows(self.item,'1','20'),'voucher','post')
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,-2);self.assertEqual(self.item.average_cost,20)
        save_purchase(header(self.vendor),rows(self.item,'3','30'),'voucher','post')
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,1);self.assertEqual(self.item.average_cost,30)


class ItemNumberConcurrencyTests(TransactionTestCase):
    def test_simultaneous_first_allocations_have_distinct_numbers(self):
        if connection.vendor!='postgresql':self.skipTest('Requires PostgreSQL row locking')
        Item.objects.create(sku='16962',name='Original')
        barrier=Barrier(2)
        def create():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    number=next_item_number()
                    Item.objects.create(sku=number,name='Copied item')
                return number
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            result=list(workers.map(lambda _:create(),range(2)))
        self.assertEqual(set(result),{'16963','16964'})
