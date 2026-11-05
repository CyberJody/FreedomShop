# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid
from decimal import Decimal as D
from django.test import TestCase
from customers.models import Customer
from purchasing.models import Vendor, PurchaseDocument, ReceivingMovement
from purchasing.services import save_purchase
from purchasing.tests import header, rows
from purchasing.views import row_initial
from .models import Item, Document, OrderPayment, StockMovement
from .services import save_document, save_order_payment, fulfill_order, WorkflowError
from .views import document_data


class TestingWorkflows(TestCase):
    def setUp(self):
        self.customer=Customer.objects.create(source_id='testing',first_name='Pat & Taylor',last_name='Example',full_name='Pat & Taylor Example')
        self.vendor=Vendor.objects.create(code='V',company='Furniture Vendor')
        self.item=Item.objects.create(sku='CHAIR',name='Chair',description='Oak chair',price=100,on_hand=0,average_cost=0,order_cost=40,tax_rate=7,primary_vendor=self.vendor)

    def order(self, qty=2):
        return save_document(dict(action='save',customer_id=self.customer.pk,lines=[dict(item_id=self.item.pk,quantity=str(qty),price='100')]),'order',uuid.uuid4())

    def pay(self, order, amount, method='cash', **extra):
        order.refresh_from_db()
        data=dict(version=order.version,token=str(uuid.uuid4()),amount=str(amount),method=method,reference='Test payment');data.update(extra)
        result=save_order_payment(order.pk,data);order.refresh_from_db();return result

    def pickup(self,order,qty):
        order.refresh_from_db()
        return fulfill_order(order.pk,dict(version=order.version,token=str(uuid.uuid4()),**{f'qty_{order.lines.get().pk}':str(qty)}))

    def test_order_deposit_procurement_partial_receiving_pickup_balance_and_history(self):
        order=self.order()
        self.pay(order,'53.50','credit')
        self.assertEqual(order.balance_due,D('160.50'))
        po=save_purchase(header(self.vendor),rows(self.item,'2','40',sales_line=order.lines.get().pk),'po','save',source_order=order)
        voucher=save_purchase(header(self.vendor),rows(self.item,'1','40',source_line=po.lines.get().pk),'voucher','post',source_po=po)
        with self.assertRaisesMessage(WorkflowError,'Record another'): self.pickup(order,1)
        self.pay(order,'53.50','cash')
        receipt=self.pickup(order,1)
        order.refresh_from_db();self.item.refresh_from_db()
        self.assertEqual((order.paid,order.balance_due,order.unfilled,self.item.on_hand),(D('107'),D('107'),D('1'),D('0')))
        self.assertEqual(receipt.source_order,order)
        self.assertEqual(receipt.payment_label,'Order payments: Credit card, Cash')
        save_purchase(header(self.vendor),rows(self.item,'1','40',source_line=po.lines.get().pk),'voucher','post',source_po=po)
        self.pay(order,'107','check',check_number='501');self.pickup(order,1)
        order.refresh_from_db();po.refresh_from_db();self.item.refresh_from_db()
        self.assertEqual((order.status,order.balance_due,order.unfilled,po.status,self.item.on_hand),('completed',0,0,'closed',0))
        self.assertContains(self.client.get('/pos/history/'),'Credit card, Cash, Check')
        self.assertContains(self.client.get('/pos/orders/?status=completed'),order.number)
        self.assertContains(self.client.get(f'/pos/order/{order.token}/'),'Payments and Pickup')
        self.assertEqual(OrderPayment.objects.count(),3)

    def test_multiple_orders_feed_one_po_and_keep_links_when_edited(self):
        first=self.order();second=self.order()
        response=self.client.get('/purchasing/po/new/',{'order':[first.token,second.token]})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.context['formset'].forms),2)
        data=rows(self.item,'2',sales_line=first.lines.get().pk)+rows(self.item,'1',sales_line=second.lines.get().pk)
        po=save_purchase(header(self.vendor),data,'po','save')
        links=list(po.lines.values_list('pk','sales_line_id'))
        revised=[dict(item=self.item,quantity=l.quantity,cost=l.cost,line_id=l.pk,sales_line=l.sales_line_id) for l in po.lines.all()]
        revised[1]['quantity']=D('2')
        po=save_purchase(header(self.vendor,token=po.token,version=po.version,notes='Both customers'),revised,'po','save')
        self.assertEqual(list(po.lines.values_list('pk','sales_line_id')),links)
        self.assertContains(self.client.get(f'/purchasing/po/{po.token}/'),second.number)
        with self.assertRaisesMessage(WorkflowError,'already covered'):
            save_purchase(header(self.vendor),rows(self.item,'1',sales_line=second.lines.get().pk),'po','save')

    def test_posted_receiving_corrections_and_cancellation_apply_only_difference(self):
        # (matching QB): a received voucher is not edited in place; it is reversed and the items received again.
        po=save_purchase(header(self.vendor),rows(self.item,'5','40'),'po','save')
        voucher=save_purchase(header(self.vendor),rows(self.item,'3','40',source_line=po.lines.get().pk),'voucher','post',po)
        line=voucher.lines.get()
        data=rows(self.item,'2','45',source_line=po.lines.get().pk,line_id=line.pk)
        with self.assertRaisesMessage(WorkflowError,'already received'):
            save_purchase(header(self.vendor,token=voucher.token,version=voucher.version),data,'voucher','post',po)
        save_purchase({'token':voucher.token,'version':voucher.version},[],'voucher','cancel',po)
        self.item.refresh_from_db();po.refresh_from_db()
        self.assertEqual((self.item.on_hand,po.due),(0,5));self.assertFalse(ReceivingMovement.objects.exists())
        voucher=save_purchase(header(self.vendor),rows(self.item,'2','45',source_line=po.lines.get().pk),'voucher','post',po)
        self.item.refresh_from_db();po.refresh_from_db()
        self.assertEqual((self.item.on_hand,self.item.average_cost,po.due),(2,45,3))
        voucher=save_purchase({'token':voucher.token,'version':voucher.version},[],'voucher','cancel',po)
        self.item.refresh_from_db();po.refresh_from_db()
        self.assertEqual((self.item.on_hand,po.due),(0,5));self.assertFalse(ReceivingMovement.objects.exists())

    def test_receipt_edit_cancel_and_payment_corrections(self):
        self.item.on_hand=5;self.item.save()
        data=dict(action='complete',customer_id=self.customer.pk,payment_method='debit',tendered='107',lines=[dict(item_id=self.item.pk,quantity='1',price='100')])
        receipt=save_document(data,'sale',uuid.uuid4())
        change=document_data(receipt);change.update(action='complete',payment_method='credit',tendered='214',payments=[{'method':'credit','amount':'214'}]);change['lines'][0]['quantity']='2'
        receipt=save_document(change,'sale',receipt.token)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,3);self.assertEqual(StockMovement.objects.get().quantity,-2)
        save_document(dict(action='cancel',version=receipt.version),'sale',receipt.token)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,5)
        order=self.order();self.pay(order,50);payment=order.payments.get()
        self.pay(order,40,'credit',payment_id=payment.pk)
        self.assertEqual(order.paid,40)
        self.pay(order,0,action='delete',payment_id=payment.pk)
        self.assertEqual(order.paid,0)

    def test_item_delete_routes_to_items_trash(self):
        # The testing-era single-item Delete now opens the Items Trash window; trashing itself is Admin Mode
        # only and is covered in sales.test_items_trash.
        copy=Item.objects.create(sku='OLD1',name='Old Chair',description='Oak chair',price=None,tax_rate=7)
        self.assertRedirects(self.client.get(f'/records/items/{copy.pk}/delete/'),f'/purchasing/items/delete/?item={copy.pk}',fetch_redirect_response=False)
        copy.refresh_from_db();self.assertTrue(copy.active)
        copy.active=False;copy.save(update_fields=['active'])
        self.assertContains(self.client.get('/purchasing/items/?status=inactive'),copy.name)
        self.assertNotContains(self.client.get('/purchasing/items/'),copy.name)

    def test_delete_customer_and_vendor_use_records_url(self):
        # Go-live cleanup: the Delete buttons post to /records/..., the old /testing/... path is gone.
        page=self.client.get(f'/records/customers/{self.customer.pk}/delete/')
        self.assertContains(page,'Delete Pat')
        self.assertNotContains(page,'Test Record')
        self.assertRedirects(self.client.post(f'/records/customers/{self.customer.pk}/delete/',{'version':self.customer.version}),'/customers/',fetch_redirect_response=False)
        self.customer.refresh_from_db();self.assertFalse(self.customer.active)
        self.assertRedirects(self.client.post(f'/records/vendors/{self.vendor.pk}/delete/',{'version':self.vendor.version}),'/purchasing/vendors/',fetch_redirect_response=False)
        self.vendor.refresh_from_db();self.assertFalse(self.vendor.active)
        self.assertEqual(self.client.get(f'/testing/customers/{self.customer.pk}/delete/').status_code,404)
        self.assertContains(self.client.get(f'/customers/{self.customer.pk}/edit/'),f'/records/customers/{self.customer.pk}/delete/')
        self.assertContains(self.client.get(f'/purchasing/vendors/{self.vendor.pk}/'),f'/records/vendors/{self.vendor.pk}/delete/')

    def test_multiselection_routes_and_customer_search(self):
        second=Item.objects.create(sku='TABLE',description='Table',price=500,tax_rate=7)
        for path in ['/purchasing/po/new/','/pos/order/new/','/pos/sale/new/']:
            result=self.client.get(path,{'item':[self.item.pk,second.pk]})
            self.assertEqual(result.status_code,200)
            if path.startswith('/pos'): self.assertEqual(len(result.context['initial']['lines']),2)
            else: self.assertEqual(len(result.context['formset']),2)
        self.assertEqual(self.client.get('/pos/lookup/customers/?q=Pat%20Example').json()['results'][0]['id'],self.customer.pk)
        self.assertContains(self.client.get('/purchasing/items/'),'Select Visible Items')
        self.assertNotContains(self.client.get('/'),'All test changes will be discarded')  # banner removed

    def test_missing_customer_and_invalid_payment_rollback(self):
        with self.assertRaisesMessage(WorkflowError,'customer is required'):
            save_document(dict(action='hold',lines=[dict(item_id=self.item.pk,quantity=1,price=100)]),'sale',uuid.uuid4())
        order=self.order()
        with self.assertRaises(WorkflowError):self.pay(order,'NaN')
        self.assertFalse(OrderPayment.objects.exists())

    def test_tax_exemption_can_be_removed_without_losing_line_rate(self):
        order=self.order()
        data=document_data(order);data.update(action='save',tax_exempt=True)
        order=save_document(data,'order',order.token)
        self.assertEqual(order.total,200);self.assertEqual(order.lines.get().tax_rate,7)
        data=document_data(order);data.update(action='save',tax_exempt=False)
        order=save_document(data,'order',order.token)
        self.assertEqual(order.total,214)

    def test_stale_payment_and_duplicate_pickup_do_not_apply_twice(self):
        self.item.on_hand=2;self.item.save();order=self.order()
        version=order.version
        self.pay(order,214)
        with self.assertRaisesMessage(WorkflowError,'changed'):
            save_order_payment(order.pk,dict(version=version,token=uuid.uuid4(),amount=214,method='cash'))
        data=dict(version=order.version,token=uuid.uuid4(),**{f'qty_{order.lines.get().pk}':'2'})
        receipt=fulfill_order(order.pk,data)
        with self.assertRaisesMessage(WorkflowError,'changed'):fulfill_order(order.pk,data)
        self.assertEqual(OrderPayment.objects.count(),1);self.assertEqual(Document.objects.filter(kind='sale').count(),1)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,0)
        receipt=save_document(dict(version=receipt.version,action='cancel'),'sale',receipt.token)
        order.refresh_from_db();self.item.refresh_from_db()
        self.assertEqual((order.status,order.unfilled,self.item.on_hand),('open',2,2))

    def test_special_order_mapping_through_po_deducts_received_stock_at_pickup(self):
        order=save_document(dict(action='save',customer_id=self.customer.pk,lines=[dict(description='Special chair',quantity='1',price='100')]),'order',uuid.uuid4())
        po=save_purchase(header(self.vendor),rows(self.item,'1','40',sales_line=order.lines.get().pk),'po','save',source_order=order)
        self.assertEqual(order.lines.get().item_id,self.item.pk)
        save_purchase(header(self.vendor),rows(self.item,'1','40',source_line=po.lines.get().pk),'voucher','post',po)
        self.pay(order,107);receipt=self.pickup(order,1)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,0)
        self.assertEqual(receipt.lines.get().item_id,self.item.pk)
