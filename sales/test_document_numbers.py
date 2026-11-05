# SPDX-License-Identifier: AGPL-3.0-or-later
"""independent, durable document numbers and reload-safe app links."""
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from openpyxl import load_workbook

from customers.models import Customer
from tags.models import FurnitureSet, FurnitureSetLine
from .models import Document, DocumentNumberCounter, Item, Line
from .services import save_document
from .rebuild import rebuild_from_backup
from .test_rebuild import write_fixture


class DocumentNumberTests(TestCase):
    def test_independent_series_and_repeated_allocations(self):
        order = Document.objects.create(kind='order', status='open')
        sale = Document.objects.create(kind='sale', status='completed')
        second = Document.objects.create(kind='order', status='open')
        self.assertEqual((order.number, sale.number, second.number), ('10000', '200000', '10001'))
        order.save()
        self.assertEqual(order.number_value, 10000)
        self.assertEqual(DocumentNumberCounter.objects.get(kind='order').next_number, 10002)

    def test_held_completion_and_cancellation_keep_identity(self):
        customer = Customer.objects.create(source_id='numbers', full_name='TEST numbers')
        token = uuid.uuid4()
        data = dict(action='hold', customer_id=customer.pk, lines=[dict(description='TEST', quantity='1', price='1.00', tax_rate='0')])
        held = save_document(data, 'sale', token)
        self.assertEqual((held.number, held.number_or_label, held.number_value), ('', 'Held', None))
        self.assertEqual(DocumentNumberCounter.objects.get(kind='sale').next_number, 200000)
        Document.objects.create(kind='sale', status='completed')
        data.update(action='complete', version=held.version, payments=[dict(method='cash', amount='1.00')])
        done = save_document(data, 'sale', token)
        self.assertEqual(done.number, '200001')
        cancelled = save_document(dict(action='cancel', version=done.version), 'sale', token)
        self.assertEqual(cancelled.number, '200001')

    def test_imported_and_held_never_consume_numbers(self):
        for kind, status, source_number in [('order', 'open', 5367), ('sale', 'completed', 121329), ('sale', 'held', None)]:
            doc = Document.objects.create(kind=kind, status=status, source_id=str(uuid.uuid4()), source_number=source_number)
            self.assertIsNone(doc.number_value)
            self.assertEqual(doc.number, str(source_number) if source_number is not None else '')
        self.assertEqual(dict(DocumentNumberCounter.objects.values_list('kind', 'next_number')), {'sale': 200000, 'order': 10000})

    def test_skip_legacy_numbers_and_imported_collisions(self):
        Document.objects.create(kind='sale', status='completed', number_value=200000)
        Document.objects.create(kind='sale', status='completed', source_id='import', source_number=200001)
        self.assertEqual(Document.objects.create(kind='sale', status='completed').number, '200002')
        self.assertEqual(Document.objects.create(kind='sale', status='completed').number, '200003')

    def test_uniqueness_per_kind_and_nulls(self):
        Document.objects.create(kind='order', status='open', number_value=250000)
        Document.objects.create(kind='sale', status='completed', number_value=250000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Document.objects.create(kind='order', status='open', number_value=250000)
        for _ in range(2):
            Document.objects.create(kind='sale', status='held')

    def test_allocation_rolls_back_with_document(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            Document.objects.create(kind='order', status='open')
            raise RuntimeError('rollback')
        self.assertEqual(Document.objects.create(kind='order', status='open').number, '10000')

    def test_missing_counter_is_created_lazily(self):
        DocumentNumberCounter.objects.all().delete()
        self.assertEqual(Document.objects.create(kind='sale', status='completed').number, '200000')

    def test_search_number_not_row_id_and_legacy_fallback(self):
        order = Document.objects.create(kind='order', status='open', customer_name='TEST numbered')
        other = Document.objects.create(kind='order', status='open', source_id='qb', source_number=5367)
        for number, expected in [('10000', order), ('5367', other), (f'SO-{order.pk:06d}', order)]:
            page = self.client.get('/pos/orders/', {'q': number}).context['page']
            self.assertEqual(list(page.object_list), [expected])
        self.assertEqual(self.client.get('/pos/orders/', {'q': str(order.pk)}).context['page'].paginator.count, 0)
        self.assertContains(self.client.get(f'/pos/order/{order.token}/'), '<title>Sales Order #10000')

    def test_number_column_sorts_issued_numbers_not_database_ids(self):
        legacy = Document.objects.create(kind='order', status='open', number_value=633239)
        native = Document.objects.create(kind='order', status='open')
        imported = Document.objects.create(kind='order', status='open', source_id='sort-qb', source_number=5367)
        page = self.client.get('/pos/orders/', {'sort': 'display_number'}).context['page']
        self.assertEqual(list(page.object_list), [imported, native, legacy])

    def test_held_list_detail_and_scroll_use_label(self):
        doc = Document.objects.create(kind='sale', status='held', customer_name='TEST held')
        for url in ['/pos/held/', f'/pos/sale/{doc.token}/', f'/pos/held/{doc.token}/details/']:
            response = self.client.get(url)
            self.assertContains(response, 'Held')
            self.assertNotContains(response, f'H-{doc.pk:06d}')
        from .views import sales_row
        doc.item_count, doc.item_quantity = 0, 0
        self.assertEqual(sales_row(doc, 'held')['cells'][1][0]['text'], 'Held')

    def test_print_renderers_use_plain_numbers(self):
        from .printing import Receipt, render_receipt
        from .letter_printing import Pages, render_sales_order
        sale = Document.objects.create(kind='sale', status='completed', completed_at=timezone.now())
        order = Document.objects.create(kind='order', status='open')
        with mock.patch.object(Receipt, 'columns') as columns:
            render_receipt(sale)
        self.assertIn('Sales Receipt #200000', str(columns.call_args_list))
        self.assertNotIn('R-200000', str(columns.call_args_list))
        with mock.patch.object(Pages, 'text') as text:
            render_sales_order(order)
        self.assertIn('Sales Order #10000', str(text.call_args_list))
        self.assertNotIn('SO-10000', str(text.call_args_list))
        held = Document.objects.create(kind='sale', status='held')
        with mock.patch.object(Receipt, 'columns') as columns:
            render_receipt(held)
        self.assertIn('Held Receipt', str(columns.call_args_list))
        self.assertNotIn('Held Receipt #', str(columns.call_args_list))

    def test_reports_and_excel_use_stored_number(self):
        from reports import general_sales, ny_periods, sales_by_vendor, sales_tax, tabular_exports
        today = timezone.localdate()
        doc = Document.objects.create(kind='sale', status='completed', completed_at=timezone.now(), subtotal=1, tax=0, total=1)
        item = Item.objects.create(sku='numbers', name='TEST', price=1)
        Line.objects.create(document=doc, item=item, position=0, description='TEST', quantity=1, price=1, tax_rate=0)
        period = ny_periods.resolve('month', today.strftime('%Y-%m'), today)
        report = general_sales.build(period)
        self.assertEqual(report.groups[0].rows[0]['receipt'], '200000')
        self.assertEqual(sales_by_vendor.build(today, today).groups[0].rows[0].receipt, '200000')
        self.assertEqual(sales_tax.build(period, detailed=True).sections[0].rows[0].receipt, '200000')
        book = load_workbook(BytesIO(tabular_exports.xlsx_bytes(report)))
        values = [str(c.value) for row in book.active for c in row]
        self.assertIn('200000', values)
        self.assertNotIn('R-200000', values)


class ReloadNumberTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='r18-reload-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'current.sqlite'
        write_fixture(self.path)
        self.item = Item.objects.create(sku='1001', name='Old name', price=1)
        tag = FurnitureSet.objects.create(name='TEST reload')
        self.line = FurnitureSetLine.objects.create(tag_set=tag, item=self.item, item_name='Old name')
        FurnitureSetLine.objects.create(tag_set=tag, sku='missing')
        FurnitureSetLine.objects.create(tag_set=tag)  # no identity must not guess by name

    def test_rebuild_relinks_sku_and_resets_testing_ranges(self):
        DocumentNumberCounter.objects.all().delete()
        result = rebuild_from_backup(self.path, commit=True)
        self.line.refresh_from_db()
        self.assertEqual((self.line.sku, self.line.item.sku, self.line.item.name), ('1001', '1001', 'Oak Chair'))
        self.assertNotEqual(self.line.item_id, self.item.pk)
        self.assertEqual(result['document_number_counters'], {'sale': 200000, 'order': 10000})
        self.assertEqual((result['counts']['relinked_tag_lines'], result['counts']['unmatched_tag_lines'], result['counts']['tag_lines_without_item_number']), (1, 1, 1))  # a real miss vs a line that never had an item number
        self.assertFalse(Document.objects.exclude(number_value=None).exists())

    def test_cutover_continues_qb_and_dry_run_rolls_back_everything(self):
        before = dict(DocumentNumberCounter.objects.values_list('kind', 'next_number'))
        result = rebuild_from_backup(self.path, continue_qb_numbers=True)
        self.assertFalse(result['committed'])
        self.assertEqual(result['document_number_counters'], {'sale': 106, 'order': 8})
        self.line.refresh_from_db()
        self.assertEqual((self.line.item_id, self.line.sku), (self.item.pk, '1001'))
        self.assertEqual(dict(DocumentNumberCounter.objects.values_list('kind', 'next_number')), before)
        result = rebuild_from_backup(self.path, commit=True, continue_qb_numbers=True)
        self.assertEqual(Document.objects.create(kind='order', status='open').number, '8')
        self.assertEqual(Document.objects.create(kind='sale', status='completed').number, '106')


class DocumentNumberConcurrencyTests(TransactionTestCase):
    def test_two_registers_allocate_distinct_numbers_even_on_first_use(self):
        if not connection.features.has_select_for_update:
            self.skipTest('Requires PostgreSQL row locks; sequential allocation is covered on SQLite.')
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        for seeded in (False, True):
            Document.objects.all().delete()
            DocumentNumberCounter.objects.all().delete()
            if seeded:
                DocumentNumberCounter.objects.create(kind='sale', next_number=200000)
            barrier = Barrier(2)
            def allocate(_):
                try:
                    barrier.wait(timeout=10)
                    return Document.objects.create(kind='sale', status='completed').number_value
                finally:
                    connections.close_all()
            with ThreadPoolExecutor(max_workers=2) as pool:
                numbers = list(pool.map(allocate, range(2)))
            self.assertEqual(sorted(numbers), [200000, 200001])
            self.assertEqual(DocumentNumberCounter.objects.get(kind='sale').next_number, 200002)


class DocumentNumberMigrationTests(TransactionTestCase):
    def test_legacy_backfill_and_tag_snapshot(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [('sales', '0022_recorded_line_amount'), ('tags', '0001_initial')]
        executor.migrate(old_targets)
        try:
            old = executor.loader.project_state(old_targets).apps
            Doc = old.get_model('sales', 'Document')
            order = Doc.objects.create(kind='order', status='open')
            sale = Doc.objects.create(kind='sale', status='completed')
            void = Doc.objects.create(kind='sale', status='cancelled', was_completed=True)
            held = Doc.objects.create(kind='sale', status='held')
            qb = Doc.objects.create(kind='order', status='open', source_id='qb', source_number=5367)
            item = old.get_model('sales', 'Item').objects.create(sku='6045', price=1)
            tag = old.get_model('tags', 'FurnitureSet').objects.create(name='TEST')
            line = old.get_model('tags', 'FurnitureSetLine').objects.create(tag_set=tag, item=item)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            for doc in [order, sale, void]:
                self.assertEqual(Document.objects.get(pk=doc.pk).number_value, doc.pk)
            for doc in [held, qb]:
                self.assertIsNone(Document.objects.get(pk=doc.pk).number_value)
            self.assertEqual(FurnitureSetLine.objects.get(pk=line.pk).sku, '6045')
            self.assertEqual(dict(DocumentNumberCounter.objects.values_list('kind', 'next_number')), {'sale': 200000, 'order': 10000})
        finally:
            MigrationExecutor(connection).migrate(latest)
