# SPDX-License-Identifier: AGPL-3.0-or-later
"""Printing parity for the demo identity in seeds/test_shop.json, retaining 7 % / Exempt / March and rounding cases.

The original recording preceded Business Settings. Item 3 re-recorded only identity changes after restoring the
original identity in memory reproduced all 25 original hashes. The demo shop rename was re-recorded the same way:
the previous seed identity reproduced all 25 hashes first. Each entry is a SHA-256 of what prints: raster images
pixel for pixel, PDFs page bitmaps + text + title/author, XLSX every cell. The stronger artifact comparison
lives in test_artifact_parity.py; recording is a separate explicit command
(python -m shop_settings.record_print_parity OUTPUT.json). The clock is frozen. TEST data only."""
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from django.test import TestCase

GOLDEN = Path(__file__).resolve().parent / 'parity_golden.json'
NOW = datetime(2026, 9, 15, 15, 30, tzinfo=dt_timezone.utc)


def image_hash(image):
    return hashlib.sha256(f'{image.mode}{image.size}'.encode() + image.tobytes()).hexdigest()


def pdf_hash(data):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    try:
        digest = hashlib.sha256(json.dumps({k: v for k, v in pdf.get_metadata_dict().items() if k in ('Title', 'Author')}, sort_keys=True).encode())
        for page in pdf:
            digest.update(page.get_textpage().get_text_range().encode())
            digest.update(image_hash(page.render(scale=0.75).to_pil()).encode())
        return digest.hexdigest()
    finally:
        pdf.close()


def xlsx_hash(data):
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data))
    digest = hashlib.sha256()
    for sheet in book.worksheets:
        digest.update(sheet.title.encode())
        for row in sheet.iter_rows(values_only=True):
            digest.update(repr(row).encode())
    return digest.hexdigest()


class PrintParityTests(TestCase):
    def setUp(self):
        patcher = mock.patch('django.utils.timezone.now', return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def outputs(self):
        from customers.models import Customer
        from purchasing.models import Vendor
        from purchasing.tests import header, rows
        from purchasing.services import save_purchase
        from reports import customer_balance, daily_totals, daily_totals_exports, exports, general_sales, inventory_exports, inventory_on_hand
        from reports import sales_by_vendor, sales_tax, sales_tax_exports, tabular_exports, tax_periods
        from sales import label_printing, letter_printing, print_documents, printing
        from sales.models import Item, Printer
        from sales.services import save_document, save_order_payment
        from tags import sets
        from tags.models import FurnitureSet, FurnitureSetLine
        from purchasing import piece_labels, print_exports

        out = {}
        customer = Customer.objects.create(source_id='TEST-parity', first_name='Pat', last_name='Buyer', full_name='Pat Buyer',
                                           street='1 Main St', city='Springfield', state='NY', postal_code='12000', phone='555-0100')
        vendor = Vendor.objects.create(code='TESTV', company='TEST Vendor', street='9 Mill Rd', city='Millbrook', state='PA', postal_code='15000')
        item = Item.objects.create(sku='TEST-P1', name='TEST Chair', description='Oak side chair', price=D('249.95'), on_hand=5, tax_rate=7,
                                   primary_vendor=vendor, wood='OAK', size='18"', order_cost=D('100'), average_cost=D('100'))
        gift = Item.objects.create(sku='TEST-P2', name='TEST Basket', description='Basket', price=D('20.00'), on_hand=5, tax_rate=0)
        sale = save_document(dict(action='complete', customer_id=customer.pk, lines=[dict(item_id=item.pk, quantity='2', price='249.95', tax_rate='7'),
                                                                                    dict(item_id=gift.pk, quantity='1', price='20.00', tax_rate='0')],
                                  payments=[dict(method='cash', amount='600.00')]), 'sale', uuid.uuid4())
        exempt = save_document(dict(action='complete', customer_id=customer.pk, tax_exempt=True, lines=[dict(item_id=item.pk, quantity='1', price='249.95')],
                                    payments=[dict(method='cash', amount='249.95')]), 'sale', uuid.uuid4())
        out['receipt'] = image_hash(printing.render_receipt(sale))
        out['receipt_reprint'] = image_hash(printing.render_receipt(sale, reprint=True))
        out['receipt_exempt'] = image_hash(printing.render_receipt(exempt))
        out['receipt_copies'] = printing.receipt_copies(sale)
        printer = Printer(name='TEST Star', kind='receipt', host='192.0.2.10', port=9100, model='TSP143', location='Counter')
        out['receipt_test_page'] = image_hash(printing.render_test_page(printer))
        out['letter_test_page'] = hashlib.sha256(b''.join(image_hash(p).encode() for p in letter_printing.render_letter_test_page(Printer(name='TEST Brother', kind='letter', host='192.0.2.30', port=631)))).hexdigest()
        order = save_document(dict(action='save', customer_id=customer.pk, notes='TEST order', lines=[dict(item_id=item.pk, quantity='1', price='249.95', tax_rate='7')]),
                              'order', uuid.uuid4())
        save_order_payment(order.pk, dict(version=order.version, token=str(uuid.uuid4()), amount='100.00', method='cash', reference='Deposit'))
        order.refresh_from_db()
        out['sales_order'] = hashlib.sha256(b''.join(image_hash(p).encode() for p in letter_printing.render_sales_order(order))).hexdigest()
        out['receipt_pdf'] = pdf_hash(print_documents.pdf(sale, 'sale', 'Sales Receipt'))
        out['order_pdf'] = pdf_hash(print_documents.pdf(order, 'order', 'Sales Order'))
        out['zebra_tag'] = image_hash(label_printing.render_tag(label_printing.prefill(item)))
        out['letter_tag'] = pdf_hash(letter_printing.tag_pdf(item, 1))
        tag_set = FurnitureSet.objects.create(name='TEST Set', tagline='Solid wood\nMade to last')
        FurnitureSetLine.objects.create(tag_set=tag_set, item=item, quantity=2)
        out['set_tag'] = pdf_hash(sets.pdf_bytes(tag_set))
        # PO numbers come from the row id. PostgreSQL sequences survive rollback, so restart this one (next id = max + 1)
        # for the same PO number on every run and backend; SQLite needs nothing.
        from django.core.management.color import no_style
        from django.db import connection
        from purchasing.models import PurchaseDocument
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [PurchaseDocument]):
                cursor.execute(sql)
        po = save_purchase(header(vendor, notes='Call first', terms='Net 30'), rows(item, '3', '100'), 'po', 'save')
        out['po_pdf'] = pdf_hash(print_exports.pdf_bytes(po))
        labels = [dict(name='TEST Chair', description='Oak side chair', quantity=2, piece=n, po_number=po.number, code=code,
                       url=piece_labels.piece_url(code), item_number='TEST-P1') for n, code in ((1, 'WC-ABCD-EFG2'), (2, 'WC-HJKM-NPQ3'))]
        out['piece_labels'] = pdf_hash(piece_labels.pdf_bytes(labels))
        day = NOW.astimezone().date()
        period = tax_periods.resolve('custom', '', day, day, day, start_month=3)
        tax_report = sales_tax.build(period, detailed=True)
        out['sales_tax_pdf'] = pdf_hash(sales_tax_exports.pdf_bytes(tax_report))
        out['sales_tax_xlsx'] = xlsx_hash(sales_tax_exports.xlsx_bytes(tax_report))
        totals = daily_totals.build(period)
        out['daily_totals_pdf'] = pdf_hash(daily_totals_exports.pdf_bytes(totals))
        out['daily_totals_xlsx'] = xlsx_hash(daily_totals_exports.xlsx_bytes(totals))
        vendor_report = sales_by_vendor.build(day, day)
        out['sales_by_vendor_pdf'] = pdf_hash(exports.pdf_bytes(vendor_report))
        out['sales_by_vendor_xlsx'] = xlsx_hash(exports.xlsx_bytes(vendor_report))
        general = general_sales.build(period)
        out['general_sales_pdf'] = pdf_hash(tabular_exports.pdf_bytes(general))
        out['general_sales_xlsx'] = xlsx_hash(tabular_exports.xlsx_bytes(general))
        balances = customer_balance.build(show='all')
        out['customer_balance_pdf'] = pdf_hash(tabular_exports.pdf_bytes(balances))
        inventory = inventory_on_hand.build()
        out['inventory_pdf'] = pdf_hash(inventory_exports.pdf_bytes(inventory))
        out['inventory_xlsx'] = xlsx_hash(inventory_exports.xlsx_bytes(inventory))
        return out

    def test_printouts_match_the_recording(self):
        out = self.outputs()
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(sorted(out), sorted(golden))
        for name in golden:
            self.assertEqual(out[name], golden[name], name)
