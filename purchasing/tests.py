# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db.models import F
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal
from django.db import connection, close_old_connections
from django.test import TestCase, TransactionTestCase, Client, override_settings
from sales.models import Item, Document, Line
from sales.services import WorkflowError, save_document
from customers.models import Customer
from .models import Vendor, Department, PurchaseDocument, ReceivingMovement
from .services import save_purchase


def header(vendor, **kw):
    data=dict(token=uuid.uuid4(),version=0,vendor=vendor,status='open',notes='',terms='',ship_date=None,payment_due=None,invoice_number='')
    data.update(kw);return data


def rows(item, quantity='2', cost='20', **kw):
    return [dict(item=item,quantity=Decimal(quantity),cost=Decimal(cost),**kw)]


class PurchasingTests(TestCase):
    def setUp(self):
        self.vendor=Vendor.objects.create(code='TEST',company='Test Vendor')
        self.item=Item.objects.create(sku='TEST-ITEM',description='Test item',price=50,on_hand=2,average_cost=10,order_cost=20,primary_vendor=self.vendor,tax_rate=7)

    def test_partial_receiving_hold_post_cost_and_idempotency(self):
        po=save_purchase(header(self.vendor),rows(self.item,'4'),'po','save')
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,2)
        line=po.lines.get()
        h=header(self.vendor)
        r=rows(self.item,source_line=line.pk)
        voucher=save_purchase(h,r,'voucher','hold',po)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,2)
        h['version']=voucher.version
        voucher=save_purchase(h,r,'voucher','post',po)
        with self.assertRaisesMessage(WorkflowError,'already received'):
            save_purchase(h,r,'voucher','post',po)
        self.item.refresh_from_db();po.refresh_from_db()
        self.assertEqual(self.item.on_hand,4)
        self.assertEqual(self.item.average_cost,15)
        self.assertEqual(self.item.order_cost,20)
        self.assertEqual(po.status,'open');self.assertEqual(po.due,2)
        self.assertEqual(ReceivingMovement.objects.count(),1)
        save_purchase(header(self.vendor),r,'voucher','post',po)
        po.refresh_from_db();self.assertEqual(po.status,'closed')
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,6)

    def test_over_receiving_vendor_mismatch_and_unknown_stock_rollback(self):
        po=save_purchase(header(self.vendor),rows(self.item),'po','save')
        with self.assertRaisesMessage(WorkflowError,'remaining'):
            save_purchase(header(self.vendor),rows(self.item,'3',source_line=po.lines.get().pk),'voucher','post',po)
        other=Vendor.objects.create(code='OTHER',company='Other')
        with self.assertRaises(WorkflowError):
            save_purchase(header(other),rows(self.item,source_line=po.lines.get().pk),'voucher','post',po)
        self.item.on_hand=None;self.item.save()
        with self.assertRaisesMessage(WorkflowError,'has no stock count yet'):
            save_purchase(header(self.vendor),rows(self.item),'voucher','post')
        self.assertEqual(PurchaseDocument.objects.count(),1)
        self.assertEqual(ReceivingMovement.objects.count(),0)

    def test_held_and_cancelled_vouchers_preserve_stock_and_stale_edits_fail(self):
        h=header(self.vendor);v=save_purchase(h,rows(self.item),'voucher','hold')
        with self.assertRaisesMessage(WorkflowError,'Someone changed'):
            save_purchase(h,rows(self.item),'voucher','hold')
        h['version']=v.version
        save_purchase(h,[],'voucher','cancel')
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,2)
        with self.assertRaisesMessage(WorkflowError,'was cancelled'):
            save_purchase(h,rows(self.item),'voucher','post')

    def test_generate_po_and_prevent_duplicate_procurement(self):
        customer=Customer.objects.create(source_id='test',full_name='Customer')
        order=Document.objects.create(kind='order',status='open',customer=customer,customer_name='Customer')
        line=Line.objects.create(document=order,position=0,item=self.item,description='Test item',quantity=3,price=50)
        po=save_purchase(header(self.vendor),rows(self.item,sales_line=line.pk),'po','save',source_order=order)
        self.assertEqual(po.source_order_id,order.pk)
        with self.assertRaisesMessage(WorkflowError,'already covered'):
            save_purchase(header(self.vendor),rows(self.item,sales_line=line.pk),'po','save',source_order=order)
        from sales.views import document_data
        data=document_data(order);data['action']='save';data['notes']='Updated linked order'
        updated=save_document(data,'order',order.token)
        self.assertEqual(updated.notes,'Updated linked order')
        self.assertEqual(po.lines.get().sales_line_id,line.pk)
        save_purchase(header(self.vendor),rows(self.item,'1',sales_line=line.pk),'po','save',source_order=order)
        response=self.client.get(f'/purchasing/po/new/?order={order.token}')
        self.assertContains(response,'No remaining items')

    def test_catalog_validation_stale_edit_and_editable_stock(self):
        data=dict(sku=self.item.sku,name='Test Item',item_type='inventory',description='Updated',price='50',order_cost='20',on_hand='999',average_cost='999',tax_rate='7',active='on',version=1)
        response=self.client.post(f'/purchasing/items/{self.item.pk}/',data)
        self.assertEqual(response.status_code,302)
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,999);self.assertEqual(self.item.average_cost,999)
        self.assertEqual(self.client.post(f'/purchasing/items/{self.item.pk}/',data).status_code,400)
        data['version']=2;data['tax_rate']='-1'
        self.assertEqual(self.client.post(f'/purchasing/items/{self.item.pk}/',data).status_code,400)
        self.assertEqual(self.client.post('/purchasing/vendors/new/',{'code':'ZIP','company':'ZIP Vendor','postal_code':'01234','active':'on'}).status_code,302)
        self.assertEqual(Vendor.objects.get(code='ZIP').postal_code,'01234')

    def test_routes_formset_save_and_po_links(self):
        for path in ['po/new','voucher/new','orders','history','held','items','vendors','departments','items/new','vendors/new','departments/new']:
            with self.subTest(path=path):self.assertEqual(self.client.get('/purchasing/'+path+'/').status_code,200)
        data={'token':str(uuid.uuid4()),'version':'0','vendor':self.vendor.pk,'status':'open','lines-TOTAL_FORMS':'1','lines-INITIAL_FORMS':'0','lines-0-item':self.item.pk,'lines-0-quantity':'3','lines-0-cost':'20','action':'save'}
        response=self.client.post('/purchasing/po/new/',data)
        self.assertEqual(response.status_code,302)
        po=PurchaseDocument.objects.get()
        page=self.client.get(response.url)
        self.assertContains(page,'<a class="button" id="receive-items" href="/purchasing/voucher/new/?po=%s">Receive Items</a>' % po.token, html=False)
        self.assertContains(page,'>Receive Items (Ctrl+F7)</a>')  # in the PO's I Want to… menu, like QB
        self.assertContains(page,"e.key==='F7'")
        self.assertNotContains(page,'Receive from PO')
        self.assertContains(page,f'<h1 class="page-title">Purchase Order · {po.number}')  # heading shows the PO number (regression)
        receive=self.client.get(f'/purchasing/voucher/new/?po={po.token}')
        self.assertContains(receive,'Receiving against')
        self.assertContains(receive,'lines-0-source_line')
        self.assertIn('no-store',receive['Cache-Control'])
        self.assertEqual(self.client.get('/purchasing/items/').context['page'][0].on_order,Decimal('3'))
        data['lines-0-quantity']='-1'
        self.assertEqual(self.client.post('/purchasing/po/new/',data).status_code,400)
        # Fully received: Receive Items stays visible but greyed out, no Ctrl+F7 (QB behaviour)
        po.lines.update(received=F('quantity'))
        page=self.client.get(f'/purchasing/po/{po.token}/')
        self.assertContains(page,'<button type="button" disabled>Receive Items (Ctrl+F7)</button>')
        self.assertContains(page,'<button type="button" class="button" disabled>Receive Items</button>')
        self.assertNotContains(page,"e.key==='F7'")
        po.status='closed';po.save(update_fields=['status'])
        closed=self.client.get(f'/purchasing/po/{po.token}/').content.decode()
        self.assertNotIn('Receive Items (Ctrl+F7)',closed);self.assertNotIn('id="receive-items"',closed);self.assertNotIn('class="button" disabled>Receive Items',closed)
        self.assertEqual(self.client.post('/purchasing/history/').status_code,405)

    def test_item_details_panel(self):
        """Item List "Show Details" docks a read-only Inventory Item Detail beside the list (QB POS behavior)."""
        self.item.name='TEST NAME';self.item.average_cost=Decimal('10.4567');self.item.comments='Fragile';self.item.save()
        save_purchase(header(self.vendor),rows(self.item,'4'),'po','save')
        listing=self.client.get('/purchasing/items/')
        self.assertContains(listing,'id="item-details"')
        self.assertContains(listing,'data-item-action="details" aria-controls="item-details"')
        self.assertNotContains(listing,'data-item-action="details" disabled')
        response=self.client.get(f'/purchasing/items/{self.item.pk}/details/')
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response['Cache-Control'])
        self.assertNotContains(response,'<html')
        for text in ['TEST NAME','Item Number: TEST-ITEM','Test Vendor','$50.00','$10.46','Fragile','data-detail-popup="available"','data-detail-popup="orders"','<dt>On Order (open POs)</dt><dd>4</dd>']:
            with self.subTest(text=text):self.assertContains(response,text)
        po=PurchaseDocument.objects.get()
        orders=response.context['orders']
        self.assertEqual([(o['type'],o['number'],o['details'],o['quantity'],o['url']) for o in orders],[('Purchase Order',po.number,'Test Vendor',Decimal('4'),f'/purchasing/po/{po.token}/')])
        self.assertFalse(response.context['po_mismatch'])
        self.assertEqual(response.context['available'],Decimal('2'))  # on hand 2, no customer orders
        line=po.lines.get();line.received=Decimal('3');line.save()
        response=self.client.get(f'/purchasing/items/{self.item.pk}/details/')
        self.assertEqual(response.context['orders'][0]['quantity'],Decimal('1'))  # remaining, not ordered
        line.received=Decimal('4');line.save()
        response=self.client.get(f'/purchasing/items/{self.item.pk}/details/')
        self.assertEqual(response.context['orders'],[])  # fully received POs are not listed
        self.assertContains(response,'No open orders for this item.')
        self.assertEqual(self.client.get('/purchasing/items/999999/details/').status_code,404)
        self.assertEqual(self.client.post(f'/purchasing/items/{self.item.pk}/details/').status_code,405)

    def test_po_and_voucher_printouts(self):
        """printable PO / receiving voucher PDF and Direct Print, with the approved layout."""
        from .print_exports import build, pdf_bytes
        self.vendor.street='819 KENTUCKY AVE.';self.vendor.city='PUNXSUTAWNEY';self.vendor.state='PA';self.vendor.postal_code='15767'
        self.vendor.phone='814-427-4130';self.vendor.contact='ERVIN BYLER';self.vendor.save()
        po=save_purchase(header(self.vendor,notes='Call before delivery',terms='Net 30'),rows(self.item,'3','20.459'),'po','save')
        data=build(po)
        self.assertEqual(data['rows'][0]['cost'],Decimal('20.46'))          # rounded half-up
        self.assertEqual(data['rows'][0]['amount'],Decimal('61.38'))        # 3 x 20.46
        self.assertEqual((data['total_qty'],data['total_cost']),(Decimal('3'),Decimal('61.38')))
        self.assertEqual(data['vendor'],['Test Vendor','Attn: ERVIN BYLER','819 KENTUCKY AVE.','PUNXSUTAWNEY, PA 15767','Phone: 814-427-4130'])
        self.assertIn(('Instructions','Call before delivery'),data['footer'])
        pdf=pdf_bytes(po)
        self.assertTrue(pdf.startswith(b'%PDF'))
        response=self.client.get(f'/purchasing/po/{po.token}/pdf/')
        self.assertEqual(response['Content-Type'],'application/pdf')
        self.assertIn(f'filename="{po.number}.pdf"',response['Content-Disposition'])
        page=self.client.get(f'/purchasing/po/{po.token}/')
        self.assertContains(page,f'/purchasing/po/{po.token}/pdf/')
        self.assertContains(page,'id="doc-direct-print"')
        self.assertNotContains(page,'onclick="window.print()">Print</button>')
        self.assertEqual(self.client.post(f'/purchasing/po/{po.token}/print/',{'printer':''}).status_code,400)
        self.assertEqual(self.client.get(f'/purchasing/voucher/{po.token}/pdf/').status_code,404)   # kind must match
        voucher=save_purchase(header(self.vendor,invoice_number='INV-9'),rows(self.item,'2','10'),'voucher','post')
        vdata=build(voucher)
        self.assertEqual(vdata['title'],'Receiving Voucher')
        self.assertIn(('Vendor Invoice #','INV-9'),vdata['fields'])
        self.assertEqual(self.client.get(f'/purchasing/voucher/{voucher.token}/pdf/').status_code,200)

    @override_settings(MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'])
    def test_csrf_and_posted_voucher_is_reversed_not_held(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post('/purchasing/vendors/new/',{}).status_code,403)
        v=save_purchase(header(self.vendor),rows(self.item),'voucher','post')
        # (matching QB): a received voucher is no longer put back on hold (that un-received it silently); a
        # receiving mistake is fixed with Reverse Voucher (cancel), which takes the stock back out.
        with self.assertRaisesMessage(WorkflowError,'already received'):
            save_purchase(header(self.vendor,token=v.token,version=v.version),rows(self.item),'voucher','hold')
        page=self.client.get(f'/purchasing/voucher/{v.token}/').content.decode()
        self.assertIn('Reverse Voucher',page);self.assertNotIn('Put on Hold',page)
        save_purchase({'token':v.token,'version':v.version},[],'voucher','cancel')
        self.item.refresh_from_db();self.assertEqual(self.item.on_hand,2)
        self.assertFalse(ReceivingMovement.objects.exists())


class ConcurrentReceivingTests(TransactionTestCase):
    def test_two_vouchers_cannot_receive_the_same_po_balance_twice(self):
        if connection.vendor!='postgresql':self.skipTest('Requires PostgreSQL row locks')
        vendor=Vendor.objects.create(code='V',company='Vendor')
        item=Item.objects.create(sku='ITEM',description='Item',price=10,on_hand=0)
        po=save_purchase(header(vendor),rows(item,'1'),'po','save')
        line=po.lines.get();barrier=Barrier(2)
        def receive(_):
            close_old_connections();barrier.wait(timeout=10)
            try:
                save_purchase(header(vendor),rows(item,'1',source_line=line.pk),'voucher','post',po)
                return 'received'
            except WorkflowError:return 'unavailable'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(receive,range(2)))
        self.assertCountEqual(results,['received','unavailable'])
        item.refresh_from_db();self.assertEqual(item.on_hand,1)
        self.assertEqual(ReceivingMovement.objects.count(),1)
