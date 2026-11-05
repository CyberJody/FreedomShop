# SPDX-License-Identifier: AGPL-3.0-or-later
from decimal import Decimal
from django.test import TestCase
from sales.models import Item
from .models import Vendor, PurchaseDocument


class CatalogActionsTests(TestCase):
    def test_copy_prefills_costs_but_does_not_force_omitted_post_fields(self):
        item = Item.objects.create(sku='OLD', description='Original Item', price=25,
            on_hand=9, average_cost=11, order_cost=12, tax_rate=8)
        response = self.client.get('/purchasing/items/new/', {'copy':item.pk})
        form = response.context['form']
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(form['description'].value(), item.description)
        self.assertEqual(form['on_hand'].value(),0)
        self.assertEqual(form['order_cost'].value(),12)
        self.assertEqual(form['average_cost'].value(),11)
        self.assertEqual(form['sku'].value(), '')
        response = self.client.post(f'/purchasing/items/new/?copy={item.pk}', {
            'sku':'NEW', 'name':'New Item', 'item_type':'inventory', 'description':'New Item', 'price':'25', 'tax_rate':'8', 'active':'on', 'version':1})
        self.assertEqual(response.status_code, 302)
        copy = Item.objects.exclude(pk=item.pk).get()
        self.assertTrue(copy.sku.isdigit())
        self.assertEqual(copy.on_hand, 0)  # a new item's blank On-Hand starts at 0, never the copied count
        self.assertIsNone(copy.average_cost)
        item.refresh_from_db()
        self.assertEqual(item.on_hand, Decimal('9'))
        self.assertEqual(self.client.get('/purchasing/items/new/?copy=bad').status_code, 404)

    def test_vendor_shortcuts_prefill_terms_and_filter_by_identity(self):
        vendor = Vendor.objects.create(company='Same Name', terms='Net 30')
        other = Vendor.objects.create(company='Same Name')
        for kind in ['po','voucher']:
            response = self.client.get(f'/purchasing/{kind}/new/', {'vendor':vendor.pk})
            self.assertEqual(response.context['header']['vendor'].value(), vendor.pk)
            self.assertEqual(response.context['header']['terms'].value(), 'Net 30')
            self.assertEqual(PurchaseDocument.objects.count(), 0)
        for kind, state, section in [('po','open','orders'), ('voucher','posted','history'), ('voucher','held','held')]:
            wanted = PurchaseDocument.objects.create(kind=kind, status=state, vendor=vendor, vendor_name='Old Name')
            PurchaseDocument.objects.create(kind=kind, status=state, vendor=other, vendor_name=vendor.company)
            response = self.client.get(f'/purchasing/{section}/', {'vendor':vendor.pk, 'period':'all'})
            self.assertEqual([obj.pk for obj in response.context['page']], [wanted.pk])
            self.assertContains(response, f'name="vendor" value="{vendor.pk}"')
            self.assertEqual(self.client.get(f'/purchasing/{section}/?vendor=bad').status_code, 404)
        self.assertEqual(self.client.get('/purchasing/po/new/?vendor=bad').status_code, 404)
        vendor.active = False
        vendor.save()
        self.assertEqual(self.client.get(f'/purchasing/po/new/?vendor={vendor.pk}').status_code, 404)
