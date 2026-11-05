# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from decimal import Decimal as D
from django.test import TestCase
from django.forms.models import model_to_dict
from customers.models import Customer, CustomerAddress
from customers.forms import CustomerForm
from customers.source_details import source_details
from purchasing.models import Vendor, Department, PurchaseDocument
from purchasing.forms import ItemForm, DepartmentForm
from purchasing.services import save_purchase
from purchasing.procurement import procurement_capacity
from purchasing.tests import header, rows
from purchasing.item_views import activity
from sales.models import Item, InventoryAdjustment
from sales.services import save_document,save_order_payment,fulfill_order,WorkflowError
from sales.views import document_data


class ParityFixTests(TestCase):
    def setUp(self):
        self.customer=Customer.objects.create(source_id='parity',first_name='First',last_name='Last',full_name='First Last',street='Unique Address',postal_code='01234')
        self.vendor=Vendor.objects.create(company='Vendor',code='V',terms='Net 30')
        self.item=Item.objects.create(sku='CHAIR',name='Chair',description='Chair',price=100,tax_rate=7,on_hand=10,average_cost=40,order_cost=40,primary_vendor=self.vendor)

    def order(self,quantity=2):
        return save_document({'action':'save','customer_id':self.customer.pk,'lines':[{'item_id':self.item.pk,'quantity':quantity,'price':100}]},'order',uuid.uuid4())

    def pickup(self,order,qty):
        save_order_payment(order.pk,dict(version=order.version,token=uuid.uuid4(),amount=str(D(qty)*107),method='cash'))
        order.refresh_from_db()
        receipt=fulfill_order(order.pk,dict(version=order.version,token=uuid.uuid4(),**{f'qty_{order.lines.get().pk}':qty}))
        order.refresh_from_db()
        return receipt

    def test_partial_pickup_limits_new_procurement_in_preview_and_save(self):
        order=self.order();self.pickup(order,1)
        response=self.client.get('/purchasing/po/new/',{'order':order.token})
        self.assertEqual(response.context['formset'].initial[0]['quantity'],1)
        self.assertEqual(activity(self.item)['customer_orders'],1)
        with self.assertRaisesMessage(WorkflowError,'already covered'):
            save_purchase(header(self.vendor),rows(self.item,'2',sales_line=order.lines.get().pk),'po','save')
        save_purchase(header(self.vendor),rows(self.item,'1',sales_line=order.lines.get().pk),'po','save')
        self.assertEqual(procurement_capacity(order.lines.get()),0)

    def test_received_and_fulfilled_overlap_does_not_double_subtract_on_po_edit(self):
        order=self.order()
        po=save_purchase(header(self.vendor),rows(self.item,'2','40',sales_line=order.lines.get().pk),'po','save',source_order=order)
        save_purchase(header(self.vendor),rows(self.item,'1','40',source_line=po.lines.get().pk),'voucher','post',po)
        self.pickup(order,1);po.refresh_from_db()
        self.assertEqual(procurement_capacity(order.lines.get()),0)
        self.assertEqual(procurement_capacity(order.lines.get(),po),2)
        l=po.lines.get()
        save_purchase(header(self.vendor,token=po.token,version=po.version),rows(self.item,'2','41',sales_line=l.sales_line_id,line_id=l.pk),'po','save',source_order=order)

    def test_multi_order_po_links_in_list_search_and_receiving(self):
        first=self.order(1)
        other=Customer.objects.create(source_id='second',full_name='AnotherCustomer')
        second=self.order(1);second.customer=other;second.customer_name=other.display_name;second.save()
        po=save_purchase(header(self.vendor),rows(self.item,'1','40',sales_line=first.lines.get().pk)+rows(self.item,'1','40',sales_line=second.lines.get().pk),'po','save')
        for path in [f'/purchasing/voucher/new/?po={po.token}',f'/purchasing/po/{po.token}/','/purchasing/orders/']:
            response=self.client.get(path)
            self.assertContains(response,first.number);self.assertContains(response,second.number)
        self.assertContains(self.client.get('/purchasing/orders/?q=AnotherCustomer'),po.number)
        self.assertContains(self.client.get('/purchasing/orders/',{'order':second.token}),po.number)

    def test_cancelled_receipt_identity_and_history(self):
        receipt=save_document(dict(action='complete',customer_id=self.customer.pk,lines=[dict(item_id=self.item.pk,quantity=1,price=100)],payment_method='cash',tendered=107),'sale',uuid.uuid4())
        number=receipt.number
        receipt=save_document(dict(action='cancel',version=receipt.version),'sale',receipt.token)
        self.assertEqual(receipt.number,number)
        self.assertContains(self.client.get('/pos/history/?status=cancelled'),number)
        self.assertContains(self.client.get('/pos/history/?status=all'),number)
        self.assertNotContains(self.client.get('/pos/history/'),number)

    def test_cancelled_receiving_and_correct_message(self):
        voucher=save_purchase(header(self.vendor),rows(self.item,'1'),'voucher','post')
        response=self.client.get(f'/purchasing/voucher/{voucher.token}/')
        self.assertContains(response,'This takes its items back out of stock (and their cost back out of inventory value)')
        self.assertNotContains(response,'Posted receiving records and stock will remain unchanged')
        save_purchase(dict(token=voucher.token,version=voucher.version),[],'voucher','cancel')
        self.assertContains(self.client.get('/purchasing/history/?status=cancelled'),voucher.number)

    def test_customer_fields_company_defaults_addresses_and_source_preservation(self):
        data={k:('' if v is None else v) for k,v in model_to_dict(self.customer,fields=CustomerForm.Meta.fields).items()}
        data.update(company='Business',track_as_company='on',notes='Delivery note',alternate_phone='123',alternate_contact='Office',tax_exempt='on',tax_rate='0',customer_type='Trade',email_opt_in='on',accept_checks='on')
        data.update({'addresses-TOTAL_FORMS':'1','addresses-INITIAL_FORMS':'0','addresses-0-label':'Delivery','addresses-0-address':'Shipping address','addresses-0-is_default':'on'})
        self.assertEqual(self.client.post(f'/customers/{self.customer.pk}/edit/',data).status_code,302)
        self.customer.refresh_from_db();self.assertEqual(self.customer.display_name,'Business')
        self.assertEqual(self.customer.default_shipping_address,'Shipping address')
        lookup=self.client.get('/pos/lookup/customers/?q=Business').json()['results'][0]
        self.assertEqual(lookup['notes'],'Delivery note');self.assertTrue(lookup['tax_exempt'])
        order=self.order();self.assertEqual(order.total,200);self.assertEqual(order.shipping_address,'Shipping address')
        data.update(version=self.customer.version,**{'addresses-INITIAL_FORMS':'1','addresses-0-id':self.customer.addresses.get().pk})
        self.assertEqual(self.client.post(f'/customers/{self.customer.pk}/edit/',data).status_code,302)
        self.assertTrue(self.customer.addresses.get().is_default)

    def test_address_defaults_are_validated_together(self):
        from customers.forms import CustomerAddressFormSet
        data={'addresses-TOTAL_FORMS':'2','addresses-INITIAL_FORMS':'0'}
        for n in range(2):data.update({f'addresses-{n}-label':'Home',f'addresses-{n}-address':'Address',f'addresses-{n}-is_default':'on'})
        form=CustomerAddressFormSet(data,instance=self.customer,prefix='addresses')
        self.assertFalse(form.is_valid());self.assertIn('only one',str(form.non_form_errors()))

    def test_source_details_only_maps_verified_columns_and_boolean_values(self):
        values=source_details({'table':'Customer','columns':{'25':'Phone','28':'Notes','52':135,'55':0,'56':257,'6':1}})
        self.assertEqual(values['alternate_phone'],'Phone');self.assertEqual(values['notes'],'Notes')
        self.assertFalse(values['accept_checks']);self.assertTrue(values['track_as_company'])
        self.assertNotIn('email_opt_in',values);self.assertNotIn('tax_exempt',values)

    def test_tax_non_rate_and_department_defaults(self):
        dep=Department.objects.create(name='No tax',tax_category='Non',tax_rate=0)
        data=dict(sku='NEW',name='New',item_type='inventory',description='New',department=dep.pk,price=100,on_hand=1,average_cost=10,active='on',version=1)
        form=ItemForm(data);self.assertTrue(form.is_valid(),form.errors)
        item=form.save();self.assertEqual(item.tax_rate,0);self.assertEqual(item.tax_category,'Non')
        data.update(sku='NEXT',tax_category='Non',tax_rate='7')
        form=ItemForm(data);self.assertTrue(form.is_valid(),form.errors);self.assertEqual(form.save().tax_rate,0)
        form=DepartmentForm(dict(name='Taxless',tax_category='Non',tax_rate=7,margin=0,markup=0))
        self.assertTrue(form.is_valid(),form.errors);self.assertEqual(form.save().tax_rate,0)

    def test_split_tender_change_edit_and_reject_non_cash_overpayment(self):
        data=dict(action='complete',customer_id=self.customer.pk,lines=[dict(item_id=self.item.pk,quantity=1,price=100)],payments=[dict(method='credit',amount='100'),dict(method='cash',amount='10')])
        receipt=save_document(data,'sale',uuid.uuid4());self.assertEqual(receipt.change,3)
        self.assertEqual(receipt.payment_label,'Credit card, Cash')
        revised=document_data(receipt);revised.update(action='complete',payments=[dict(method='debit',amount='107')])
        receipt=save_document(revised,'sale',receipt.token);self.item.refresh_from_db();self.assertEqual(self.item.on_hand,9)
        data['payments']=[dict(method='credit',amount='110')]
        with self.assertRaisesMessage(WorkflowError,'Only cash'):save_document(data,'sale',uuid.uuid4())

    def test_order_overpayment_remains_open_until_refunded(self):
        order=self.order(1)
        save_order_payment(order.pk,dict(version=order.version,token=uuid.uuid4(),amount=110,method='cash',confirm_overpayment='1'))
        order.refresh_from_db()
        fulfill_order(order.pk,dict(version=order.version,token=uuid.uuid4(),**{f'qty_{order.lines.get().pk}':1}))
        order.refresh_from_db();self.assertEqual(order.status,'open');self.assertEqual(order.unused_payments,3)
        save_order_payment(order.pk,dict(version=order.version,token=uuid.uuid4(),amount=-3,method='cash'))
        order.refresh_from_db();self.assertEqual(order.status,'completed')

    def test_stock_cost_changes_have_history_and_stale_edits_do_not_add_entries(self):
        data=dict(sku=self.item.sku,name='Chair',item_type='inventory',description='Chair',price=100,tax_rate=7,on_hand=8,average_cost=45,version=1,adjustment_note='Count corrected')
        url=f'/purchasing/items/{self.item.pk}/'
        self.assertEqual(self.client.post(url,data).status_code,302)
        a=InventoryAdjustment.objects.get();self.assertEqual((a.old_quantity,a.new_quantity,a.old_cost,a.new_cost),(10,8,40,45))
        self.assertEqual(self.client.post(url,data).status_code,400);self.assertEqual(InventoryAdjustment.objects.count(),1)
        self.assertContains(self.client.get(url),'Count corrected')

    def test_search_sort_quick_pick_and_test_tag(self):
        self.assertEqual(self.client.get('/customers/?q=Unique%20Address').context['page'].paginator.count,1)
        for path in ['/customers/?sort=last_name','/purchasing/items/?sort=-price','/purchasing/items/?sort=-price&format=rows','/purchasing/vendors/?sort=company','/purchasing/departments/?sort=name','/pos/history/?sort=total','/purchasing/orders/?sort=vendor_name']:
            self.assertEqual(self.client.get(path).status_code,200,path)
        self.item.quick_pick_group='Chairs';self.item.save()
        self.assertEqual(self.client.get('/pos/lookup/items/?group=Chairs').json()['results'][0]['id'],self.item.pk)
        tag=self.client.get(f'/purchasing/items/{self.item.pk}/tag/');self.assertContains(tag,'Item Price Tag');self.assertNotContains(tag,'TESTING ENVIRONMENT')  # banner removed
        self.assertContains(self.client.get('/pos/sale/new/'),'Sell Miscellaneous Item')
        self.assertContains(self.client.get('/pos/sale/new/'),'Add Customer')
