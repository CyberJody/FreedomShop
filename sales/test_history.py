# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from .models import Document, Item, Line
from .printing import render_receipt


class ReceiptHistoryTests(TestCase):
    def receipt(self, **kwargs):
        doc = Document.objects.create(kind='sale', status='completed',
                                      completed_at=timezone.now() - timedelta(days=400),
                                      subtotal='109.90', tax='7.69', total='117.59', **kwargs)
        Line.objects.create(document=doc, position=1, sku='123', item_name='Sunflower',
                            description='Poly flower', quantity=2, price='54.95', tax_rate=7)
        doc.refresh_from_db()
        return doc

    def test_home_routes_distinguish_orders_and_receipts(self):
        home = self.client.get('/')
        tasks = {t['label']: t['url'] for t in home.context['sales_tasks']}
        self.assertEqual(tasks['Sales History'], '/pos/history/')
        self.assertEqual(tasks['Order List'], '/pos/orders/')

    def test_history_defaults_to_all_dates_and_only_completed_receipts(self):
        receipt = self.receipt()
        Document.objects.create(kind='sale', status='held')
        Document.objects.create(kind='order', status='completed')
        page = self.client.get('/pos/history/')
        self.assertEqual(list(page.context['page']), [receipt])
        self.assertEqual(page.context['period'], 'all')
        self.assertContains(page, 'Receipt History')
        self.assertEqual(self.client.get('/pos/history/?period=30').context['page'].paginator.count, 0)

    def test_order_list_includes_completed_order_history_by_default(self):
        for status in ['open', 'completed', 'cancelled']:
            Document.objects.create(kind='order', status=status)
        self.receipt()
        page = self.client.get('/pos/orders/')
        self.assertEqual(page.context['page'].paginator.count, 3)
        self.assertContains(page, 'Sales Order History')
        self.assertEqual(self.client.get('/pos/orders/?status=open').context['page'].paginator.count, 1)

    def test_random_receipt_honors_search_and_date_filters(self):
        receipt = self.receipt(customer_name='Chosen buyer')
        self.receipt(customer_name='Other buyer')
        Document.objects.create(kind='sale', status='held', customer_name='Chosen buyer')
        Document.objects.create(kind='order', status='open', customer_name='Chosen buyer')
        response = self.client.get('/pos/history/', {'q': 'Chosen', 'random': '1'})
        self.assertRedirects(response, f'/pos/sale/{receipt.token}/')
        empty = self.client.get('/pos/history/', {'period': '30', 'random': '1'})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.context['printable_count'], 0)

    def test_random_can_choose_beyond_first_page_without_printing_or_writing(self):
        receipts = [self.receipt() for _ in range(51)]
        before = list(Document.objects.values())
        with mock.patch('sales.views.secrets.randbelow', return_value=50), mock.patch('sales.printing.socket.create_connection') as printer:
            response = self.client.get('/pos/history/?random=1')
        self.assertRedirects(response, f'/pos/sale/{receipts[-1].token}/')
        printer.assert_not_called()
        self.assertEqual(list(Document.objects.values()), before)

    def test_cancelled_and_empty_receipts_are_not_random_print_candidates(self):
        receipt = self.receipt()
        receipt.status = 'cancelled'
        receipt.save()
        Document.objects.create(kind='sale', status='completed')
        page = self.client.get('/pos/history/?status=all&random=1')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['printable_count'], 0)

    def test_saved_prices_survive_missing_or_changed_catalog_price_and_reprint(self):
        receipt = self.receipt()
        item = Item.objects.create(sku='123', description='Current item', price=None, on_hand=None, tax_rate=7)
        line = receipt.lines.get()
        line.item = item
        line.save()
        for price in [None, Decimal('999.00')]:
            item.price = price
            item.save()
            page = self.client.get(f'/pos/sale/{receipt.token}/')
            self.assertEqual(page.context['initial']['lines'][0]['price'], '54.95')
            with mock.patch('sales.printing.dollars', wraps=lambda value: f'${value:,.2f}') as dollars:
                render_receipt(receipt, reprint=True)
            for amount in ['54.95', '109.90', '117.59']:
                self.assertIn(mock.call(Decimal(amount)), dollars.call_args_list)
        self.assertEqual(self.client.get('/pos/history/?q=Sunflower').context['page'].paginator.count, 1)
