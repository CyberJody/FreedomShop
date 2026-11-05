# SPDX-License-Identifier: AGPL-3.0-or-later
import io
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from openpyxl import Workbook

from customers.models import Customer
from purchasing.models import Department, PurchaseDocument, PurchaseLine, ReceivingMovement, Vendor
from reports.tax_ledger import tenders
from shop_settings.models import TaxCategory, TaxSettings

from . import qbpos_views
from .models import Document, ImportLog, Item, Line, StockMovement, Tender
from .qbpos_files import ImportError, boolean, number, parse_file, read_path
from .qbpos_import import import_exports, mapping_proposals, undo_import
from .services import WorkflowError, save_document

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'qbpos'


def payload(*kinds):
    return {kind: read_path(kind, FIXTURES / f'{kind}.csv') for kind in kinds}


class ReaderTests(SimpleTestCase):
    def test_aliases_have_no_conflicting_normalizations(self):
        from .qbpos_columns import ALIASES, heading
        for kind, columns in ALIASES.items():
            seen = {}
            for field, aliases in columns.items():
                for alias in aliases:
                    normalized = heading(alias)
                    self.assertIn(seen.get(normalized), (None, field), (kind, alias))
                    seen[normalized] = field

    def test_csv_aliases_and_unknown_columns(self):
        file = read_path('items', FIXTURES / 'items.csv')
        self.assertEqual(file['rows'][0]['quantity'], '3')
        self.assertEqual(file['rows'][0]['upc'], '001234567890')
        self.assertEqual(file['unknown_columns'], ['Custom Demo Field'])
        self.assertEqual(file['rows'][0]['_row'], 2)

    def test_cp1252_tab_and_legacy_description_aliases(self):
        file = read_path('items', FIXTURES / 'items-cp1252.txt')
        self.assertEqual(file['encoding'], 'cp1252')
        self.assertEqual(file['rows'][0]['name'], 'Café Table')
        self.assertEqual(file['rows'][0]['cost'], '30.25')

    def test_utf8_bom_and_quoted_comma(self):
        file = parse_file('items', 'demo.csv', '\ufeffItem Name,Item Description\nChair,"Small, maple"\n'.encode())
        self.assertEqual(file['rows'][0]['description'], 'Small, maple')

    def test_xlsx_preamble_and_text_upc(self):
        file = read_path('items', FIXTURES / 'items.xlsx')
        self.assertEqual(file['rows'][0]['name'], 'Demo Stool')
        self.assertEqual(file['rows'][0]['_row'], 3)
        self.assertEqual(file['rows'][0]['upc'], '001234567899')
        self.assertEqual(file['rows'][0]['sku'], '201')

    def test_duplicate_alias_columns_are_rejected(self):
        with self.assertRaisesRegex(ImportError, 'same field'):
            parse_file('items', 'demo.csv', b'Item Name,Description 1\nChair,Chair\n')

    def test_backups_and_old_xls_are_rejected(self):
        for name in ('demo.qpb', 'demo.xls', 'demo.sqlite'):
            with self.subTest(name=name), self.assertRaisesRegex(ImportError, 'backups are not accepted'):
                parse_file('items', name, b'demo')

    def test_formula_workbook_rejected(self):
        book = Workbook()
        book.active.append(['Item Name'])
        book.active.append(['=1+1'])
        stream = io.BytesIO()
        book.save(stream)
        with self.assertRaisesRegex(ImportError, 'Formulas'):
            parse_file('items', 'demo.xlsx', stream.getvalue())

    def test_formula_in_unknown_column_is_ignored(self):
        book = Workbook()
        book.active.append(['Item Name', 'Unknown formula'])
        book.active.append(['Demo Chair', '=1+1'])
        stream = io.BytesIO()
        book.save(stream)
        file = parse_file('items', 'demo.xlsx', stream.getvalue())
        self.assertEqual(file['rows'][0]['name'], 'Demo Chair')
        self.assertEqual(file['unknown_columns'], ['Unknown formula'])

    def test_invalid_numbers_rejected(self):
        for value in ('NaN', 'Infinity', '1.234', '999999999999', 'oops'):
            with self.subTest(value=value), self.assertRaises(ImportError):
                number(value, 'Price')
        self.assertEqual(number('$1,250.00', 'Price'), D('1250'))
        self.assertEqual(number('(12.00)', 'Refund'), D('-12'))

    def test_invalid_boolean_rejected(self):
        self.assertTrue(boolean('Yes'))
        self.assertFalse(boolean('No'))
        with self.assertRaises(ImportError):
            boolean('Maybe')

    def test_missing_headers_and_extra_values_rejected(self):
        with self.assertRaisesRegex(ImportError, 'headings'):
            parse_file('items', 'demo.csv', b'Hello\nDemo\n')
        with self.assertRaisesRegex(ImportError, 'more values'):
            parse_file('items', 'demo.csv', b'Item Name\nDemo,extra\n')

    def test_size_limit(self):
        with mock.patch('sales.qbpos_files.MAX_BYTES', 4), self.assertRaisesRegex(ImportError, '20 MB'):
            parse_file('items', 'demo.csv', b'12345')

    def test_corrupt_xlsx_is_a_validation_error(self):
        with self.assertRaisesRegex(ImportError, 'cannot read XLSX'):
            parse_file('items', 'demo.xlsx', b'not a workbook')

    def test_extra_whitespace_is_trimmed(self):
        file = parse_file('items', 'demo.csv', b' Item Name ,Tax Code\n Demo Chair , Tax \n')
        self.assertEqual(file['rows'][0]['name'], 'Demo Chair')
        self.assertEqual(file['rows'][0]['tax_code'], 'Tax')


class ImportTests(TestCase):
    def setUp(self):
        self.data = payload('vendors', 'items', 'customers', 'sales')

    def test_dry_run_counts_and_no_business_writes(self):
        summary = import_exports(self.data)
        self.assertEqual(summary['counts']['items']['created'], 3)
        self.assertEqual(summary['counts']['customers']['created'], 2)
        self.assertEqual(summary['counts']['vendors']['created'], 1)
        self.assertEqual(summary['counts']['departments']['created'], 2)
        self.assertEqual(summary['counts']['sales']['created'], 2)
        self.assertEqual(summary['receipts_totals_only'], 2)
        for model in (Item, Customer, Vendor, Department, Document, PurchaseDocument, ReceivingMovement, ImportLog):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_commit_and_reimport_are_idempotent(self):
        first = import_exports(self.data, commit=True)
        second = import_exports(self.data, commit=True)
        self.assertEqual(Item.objects.count(), 3)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertEqual(Document.objects.count(), 2)
        self.assertEqual(PurchaseDocument.objects.count(), 1)
        self.assertEqual(second.summary['counts']['items']['reused'], 3)
        self.assertEqual(second.changes, [])
        undo_import(second.pk)
        self.assertEqual(Item.objects.count(), 3)
        undo_import(first.pk)
        self.assertFalse(Item.objects.exists())

    def test_all_created_rows_have_provenance(self):
        log = import_exports(self.data, commit=True)
        for model in (Vendor, Department, Item, Customer, Document, PurchaseDocument, Line, PurchaseLine):
            for sid in model.objects.values_list('source_id', flat=True):
                self.assertRegex(sid, r'^qbpos:[a-f0-9]{64}:\d+')
        self.assertEqual(len(log.files), 4)
        self.assertEqual(log.payload, {})

    def test_opening_inventory_math_and_date(self):
        log = import_exports(self.data, commit=True)
        # 3 × 50 + 2 × 65.50 = 281.00. Old receipts do not replay stock.
        inventory = sum((i.on_hand * i.average_cost for i in Item.objects.all()), D('0'))
        received = sum(ReceivingMovement.objects.values_list('value', flat=True), D('0'))
        self.assertEqual(inventory, D('281.00'))
        self.assertEqual(received, inventory)
        voucher = PurchaseDocument.objects.get()
        self.assertEqual(voucher.invoice_date, timezone.localdate())
        self.assertEqual(voucher.total, inventory)
        self.assertEqual(voucher.status, 'posted')
        self.assertEqual(voucher.lines.count(), 2)
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(log.summary['opening_value'], '281.00')

    def test_one_opening_voucher_per_department(self):
        data = payload('items')
        data['items']['rows'][2]['quantity'] = '2'
        data['items']['rows'][2]['cost'] = '5'
        log = import_exports(data, commit=True)
        self.assertEqual(PurchaseDocument.objects.count(), 2)
        self.assertEqual(log.summary['opening_vouchers'], 2)

    def test_fractional_opening_values_match_existing_inventory_report(self):
        from reports.inventory_on_hand import build
        data = payload('items')
        data['items']['rows'][0]['quantity'] = '1.125'
        log = import_exports(data, commit=True)
        received = sum(ReceivingMovement.objects.values_list('value', flat=True), D('0'))
        self.assertEqual(build().value, D('187.25'))
        self.assertEqual(build().value, received)
        self.assertEqual(str(received), log.summary['opening_value'])

    def test_no_opening_voucher_choice(self):
        import_exports(self.data, {'opening_voucher': False}, commit=True)
        self.assertEqual(Item.objects.get(sku='101').on_hand, D('3'))
        self.assertFalse(PurchaseDocument.objects.exists())
        self.assertFalse(ReceivingMovement.objects.exists())

    def test_tax_and_non_codes_use_categories(self):
        import_exports(self.data, commit=True)
        taxable = Item.objects.get(sku='101')
        exempt = Item.objects.get(sku='103')
        self.assertTrue(TaxCategory.objects.get(name=taxable.tax_category).taxable)
        self.assertFalse(TaxCategory.objects.get(name=exempt.tax_category).taxable)
        self.assertEqual(taxable.tax_rate, D('7'))
        self.assertEqual(exempt.tax_rate, D('0'))
        self.assertEqual(taxable.department.tax_category.name, taxable.tax_category)
        self.assertTrue(Customer.objects.get(first_name='Robin').tax_exempt)

    def test_unknown_tax_code_needs_mapping(self):
        data = payload('items')
        data['items']['rows'][0]['tax_code'] = 'Special'
        with self.assertRaisesRegex(ImportError, 'Choose a TaxCategory'):
            import_exports(data, commit=True)
        self.assertFalse(Item.objects.exists())
        category = TaxCategory.objects.create(name='Demo reduced', taxable=True, default_rate=D('2.5'))
        import_exports(data, {'tax_map': {'Special': category.pk}}, commit=True)
        self.assertEqual(Item.objects.get(sku='101').tax_rate, D('2.5'))

    def test_tax_off_produces_zero_rates(self):
        from shop_settings.config import tax
        tax()
        settings = TaxSettings.objects.get()
        settings.charge_tax = False
        settings.save()
        import_exports(payload('items'), commit=True)
        self.assertEqual(set(Item.objects.values_list('tax_rate', flat=True)), {D('0')})

    def test_department_mapping_reuses_without_mutation(self):
        department = Department.objects.create(name='Demo existing', code='EXIST', tax_rate=D('3'))
        import_exports(payload('items'), {'department_map': {'Dining': department.pk}}, commit=True)
        self.assertEqual(Item.objects.get(sku='101').department_id, department.pk)
        department.refresh_from_db()
        self.assertEqual(department.tax_rate, D('3'))

    def test_duplicate_items_and_customers_skipped(self):
        data = payload('items', 'customers')
        item = deepcopy(data['items']['rows'][0])
        item['_row'] = 5
        item['sku'] = '104'
        data['items']['rows'].append(item)
        customer = deepcopy(data['customers']['rows'][0])
        customer['_row'] = 4
        customer['phone'] = '(555) 0100'
        data['customers']['rows'].append(customer)
        summary = import_exports(data)
        self.assertEqual(summary['counts']['items']['skipped'], 1)
        self.assertEqual(summary['counts']['customers']['skipped'], 1)
        self.assertEqual(len(summary['duplicates']), 2)

    def test_changed_file_sha_reports_duplicates(self):
        import_exports(self.data, commit=True)
        data = payload('items', 'customers')
        for file in data.values():
            file['sha256'] = 'a' * 64
        log = import_exports(data, commit=True)
        self.assertEqual(log.summary['counts']['items']['skipped'], 3)
        self.assertEqual(log.summary['counts']['customers']['skipped'], 2)
        self.assertEqual(log.changes, [])

    def test_reimport_preserves_live_stock_costs(self):
        import_exports(payload('items'), commit=True)
        Item.objects.filter(sku='101').update(on_hand=1, average_cost=55)
        log = import_exports(payload('items'), commit=True)
        item = Item.objects.get(sku='101')
        self.assertEqual(item.on_hand, D('1'))
        self.assertEqual(item.average_cost, D('55'))
        self.assertEqual(log.changes, [])

    def test_reimport_handles_photos_and_undo_refuses_a_later_photo(self):
        log = import_exports(payload('items'), commit=True)
        Item.objects.filter(sku='101').update(photo=b'demo-image')
        second = import_exports(payload('items'), commit=True)
        self.assertEqual(second.changes, [])
        self.assertEqual(bytes(Item.objects.get(sku='101').photo), b'demo-image')
        with self.assertRaisesRegex(ImportError, 'edited or used'):
            undo_import(log.pk)

    def test_item_number_collision_does_not_create_unused_dependencies(self):
        Item.objects.create(sku='101', name='Existing different chair', description='Demo', on_hand=0, tax_rate=0)
        data = payload('items')
        data['items']['rows'] = data['items']['rows'][:1]
        log = import_exports(data, commit=True)
        self.assertEqual(log.summary['counts']['items']['skipped'], 1)
        self.assertFalse(Department.objects.exists())
        self.assertFalse(Vendor.objects.exists())

    def test_reimport_updates_owned_metadata_and_undo_restores(self):
        first = import_exports(payload('items'), commit=True)
        Item.objects.filter(sku='101').update(description='Local note')
        second = import_exports(payload('items'), commit=True)
        self.assertEqual(second.summary['counts']['items']['updated'], 1)
        self.assertEqual(Item.objects.get(sku='101').description, 'Demo dining chair')
        undo_import(second.pk)
        self.assertEqual(Item.objects.get(sku='101').description, 'Local note')
        with self.assertRaises(ImportError):
            undo_import(first.pk)

    def test_undo_removes_whole_batch(self):
        log = import_exports(self.data, commit=True)
        undo_import(log.pk)
        for model in (Vendor, Department, Item, Customer, Document, Line, PurchaseDocument, PurchaseLine, ReceivingMovement):
            self.assertFalse(model.objects.exists(), model.__name__)
        log.refresh_from_db()
        self.assertEqual(log.status, 'undone')
        with self.assertRaises(ImportError):
            undo_import(log.pk)

    def test_undo_refuses_edited_stock_atomically(self):
        log = import_exports(self.data, commit=True)
        Item.objects.filter(sku='101').update(on_hand=2)
        with self.assertRaisesRegex(ImportError, 'edited or used'):
            undo_import(log.pk)
        self.assertEqual(Item.objects.count(), 3)
        self.assertEqual(Document.objects.count(), 2)

    def test_undo_refuses_new_sales_history_even_if_stock_unchanged(self):
        log = import_exports(payload('items'), commit=True)
        document = Document.objects.create(kind='sale', status='held')
        Line.objects.create(document=document, position=1, item=Item.objects.get(sku='101'), description='Demo',
                            quantity=1, price=120, tax_rate=7, unit_cost=50)
        with self.assertRaisesRegex(ImportError, 'history outside'):
            undo_import(log.pk)
        self.assertEqual(PurchaseDocument.objects.count(), 1)

    def test_undo_refuses_return_link_to_imported_receipt(self):
        log = import_exports(payload('sales'), commit=True)
        receipt = Document.objects.get(source_number=500)
        document = Document.objects.create(kind='sale', status='held')
        Line.objects.create(document=document, position=1, returned_from_line=receipt.lines.first(), description='Demo return',
                            quantity=-1, price=120, tax_rate=7, unit_cost=50)
        with self.assertRaisesRegex(ImportError, 'history outside'):
            undo_import(log.pk)
        self.assertTrue(Document.objects.filter(pk=receipt.pk).exists())

    def test_sales_totals_only_and_read_only(self):
        import_exports(self.data, commit=True)
        doc = Document.objects.get(source_number=500)
        self.assertEqual(doc.subtotal, D('145'))
        self.assertEqual(doc.tax, D('8.40'))
        self.assertEqual(doc.total, D('153.40'))
        self.assertTrue(doc.imported)
        self.assertTrue(doc.was_completed)
        self.assertEqual(timezone.localdate(doc.created_at), date(2025, 1, 15))
        self.assertEqual(doc.created_at, doc.completed_at)
        self.assertFalse(doc.tenders.exists())
        self.assertTrue(tenders(doc)[1])
        self.assertIn('Totals only', doc.notes)
        with self.assertRaisesRegex(WorkflowError, 'read-only'):
            save_document({'version': doc.version, 'action': 'complete'}, 'sale', doc.token)
        reversal = Document.objects.get(source_number=501)
        self.assertEqual(reversal.total, D('-160.50'))
        self.assertEqual(reversal.receipt_type, 1)

    def test_reconciled_explicit_tender_amounts(self):
        data = {'sales': read_path('sales', FIXTURES / 'sales-tenders.csv')}
        log = import_exports(data, commit=True)
        self.assertEqual(Tender.objects.count(), 1)
        self.assertEqual(Tender.objects.get().amount, D('155.15'))
        self.assertEqual(log.summary['receipts_with_tenders'], 1)
        self.assertFalse(tenders(Document.objects.get())[1])
        undo_import(log.pk)
        self.assertFalse(Tender.objects.exists())

    def test_recorded_totals_flow_through_existing_reports(self):
        from reports import daily_totals, sales_tax
        from reports.tax_periods import Period
        data = {'sales': read_path('sales', FIXTURES / 'sales-tenders.csv')}
        import_exports(data, commit=True)
        period = Period('custom', date(2025, 1, 15), date(2025, 1, 15), 'Demo day')
        daily, tax = daily_totals.build(period), sales_tax.build(period)
        self.assertEqual(daily.totals['received'], D('155.15'))
        self.assertEqual(daily.totals['tax'], D('10.15'))
        self.assertEqual(tax.taxable, D('145'))
        self.assertEqual(tax.total, D('155.15'))

    def test_explicit_exported_rate_does_not_recompute_recorded_tax(self):
        data = {'sales': read_path('sales', FIXTURES / 'sales-tenders.csv')}
        for row in data['sales']['rows']:
            row['tax_rate'] = '8'
        import_exports(data, commit=True)
        self.assertEqual(Document.objects.get().tax, D('10.15'))
        self.assertEqual(set(Line.objects.values_list('tax_rate', flat=True)), {D('8')})

    def test_invalid_sales_field_length_is_validation_error(self):
        data = payload('sales')
        data['sales']['rows'][0]['item'] = 'x' * 201
        with self.assertRaisesRegex(ImportError, 'validation failed'):
            import_exports(data, commit=True)
        self.assertFalse(Document.objects.exists())

    def test_unreconciled_tenders_stay_totals_only(self):
        data = {'sales': read_path('sales', FIXTURES / 'sales-tenders.csv')}
        data['sales']['rows'][0]['tender_amount'] = '12.00'
        log = import_exports(data, commit=True)
        self.assertFalse(Tender.objects.exists())
        self.assertEqual(log.summary['receipts_totals_only'], 1)

    def test_inconsistent_receipt_date_rejected_atomically(self):
        self.data['sales']['rows'][1]['date'] = '2025-01-16'
        with self.assertRaisesRegex(ImportError, 'inconsistent date'):
            import_exports(self.data, commit=True)
        self.assertFalse(Item.objects.exists())

    def test_bad_later_row_rolls_back_earlier_rows(self):
        self.data['customers']['rows'][1]['tax_exempt'] = 'maybe'
        with self.assertRaises(ImportError):
            import_exports(self.data, commit=True)
        self.assertFalse(Item.objects.exists())
        self.assertFalse(Vendor.objects.exists())

    def test_negative_stock_requires_plain_choice(self):
        data = payload('items')
        data['items']['rows'][0]['quantity'] = '-1'
        with self.assertRaisesRegex(ImportError, 'negative opening'):
            import_exports(data, commit=True)
        import_exports(data, {'opening_voucher': False}, commit=True)
        self.assertEqual(Item.objects.get(sku='101').on_hand, D('-1'))

    def test_blank_files_rejected(self):
        with self.assertRaisesRegex(ImportError, 'at least one'):
            import_exports({})


class CommandTests(TestCase):
    def test_command_preview_commit_reimport_and_undo(self):
        output = io.StringIO()
        call_command('import_qbpos', items=str(FIXTURES / 'items.csv'), stdout=output)
        self.assertIn('Dry run', output.getvalue())
        self.assertFalse(Item.objects.exists())
        call_command('import_qbpos', items=str(FIXTURES / 'items.csv'), commit=True, stdout=io.StringIO())
        log = ImportLog.objects.get()
        call_command('import_qbpos', undo=str(log.pk), commit=True, stdout=io.StringIO())
        self.assertFalse(Item.objects.exists())

    def test_command_undo_requires_commit(self):
        with self.assertRaisesRegex(CommandError, 'Use --undo'):
            call_command('import_qbpos', undo='missing')


class ViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username='demo-admin')
        self.user.groups.add(Group.objects.get_or_create(name='Admin')[0])

    def request(self, method, path, data=None, user=None):
        request = getattr(self.factory, method)(path, data or {})
        request.user = user or self.user
        return request

    def test_non_admin_cannot_upload_commit_view_logs_or_undo(self):
        user = get_user_model().objects.create_user(username='demo-register', is_staff=True, is_superuser=True)
        for view, args in ((qbpos_views.upload, ()), (qbpos_views.imports, ()),
                           (qbpos_views.preview, ('00000000-0000-0000-0000-000000000000',)),
                           (qbpos_views.undo, ('00000000-0000-0000-0000-000000000000',))):
            for method in ('get', 'post'):
                with self.subTest(view=view.__name__, method=method):
                    response = view(self.request(method, '/', user=user), *args)
                    self.assertEqual(response.status_code, 403 if method == 'post' else 302)

    def test_upload_preview_then_import_then_list_then_undo(self):
        response = qbpos_views.upload(self.request('post', '/', {'items': SimpleUploadedFile('items.csv', (FIXTURES / 'items.csv').read_bytes()), 'opening_voucher': 'on'}))
        self.assertEqual(response.status_code, 302)
        log = ImportLog.objects.get()
        self.assertEqual(log.status, 'preview')
        self.assertFalse(Item.objects.exists())
        preview = qbpos_views.preview(self.request('get', '/'), log.pk)
        self.assertContains(preview, 'Dry run')
        self.assertContains(preview, 'Custom Demo Field')
        response = qbpos_views.preview(self.request('post', '/', {'action': 'import'}), log.pk)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Item.objects.count(), 3)
        self.assertContains(qbpos_views.imports(self.request('get', '/')), '281.00')
        qbpos_views.undo(self.request('post', '/'), log.pk)
        self.assertFalse(Item.objects.exists())

    def test_undo_is_post_only(self):
        response = qbpos_views.undo(self.request('get', '/'), '00000000-0000-0000-0000-000000000000')
        self.assertEqual(response.status_code, 405)

    def test_unknown_tax_code_can_be_reviewed_before_commit(self):
        file = (FIXTURES / 'items.csv').read_bytes().replace(b',Tax,', b',Special,')
        qbpos_views.upload(self.request('post', '/', {'items': SimpleUploadedFile('items.csv', file), 'opening_voucher': 'on'}))
        log = ImportLog.objects.get()
        self.assertTrue(log.summary['error'])
        self.assertContains(qbpos_views.preview(self.request('get', '/'), log.pk), 'Choose a TaxCategory')
        category = TaxCategory.objects.filter(taxable=True).first()
        exempt = TaxCategory.objects.filter(taxable=False).first()
        qbpos_views.preview(self.request('post', '/', {'action': 'review', 'tax_0': str(category.pk),
                             'tax_1': str(exempt.pk), 'opening_voucher': 'on'}), log.pk)
        log.refresh_from_db()
        self.assertFalse(log.summary['error'])
        qbpos_views.preview(self.request('post', '/', {'action': 'import'}), log.pk)
        self.assertEqual(Item.objects.count(), 3)
