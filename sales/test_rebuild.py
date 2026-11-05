# SPDX-License-Identifier: AGPL-3.0-or-later
"""rebuild_from_backup: wipe business data and load the clean QB POS dataset (current.sqlite) into the app."""
import io
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.utils import timezone

from customers.models import Customer, CustomerAddress
from purchasing.models import Department, ItemVendor, PurchaseDocument, PurchaseLine, ReceivingMovement, Vendor
from . import rebuild
from .models import Document, InventoryAdjustment, Item, ItemNumberCounter, Line, OrderPayment, Printer, StockMovement, Tender

SHA = 'ab' * 32
SID = {name: f'{index:016x}' for index, name in enumerate(
    ['D1', 'V1', 'V2', 'C1', 'C2', 'I1', 'I2', 'DEAD', 'R1', 'R2', 'R3', 'R4', 'R5', 'SO1', 'SO2', 'SO3', 'PO1', 'PO2', 'PO3', 'VO1', 'VO2', 'VO3', 'GONE', 'SO4'], start=1)}


class FixtureWriter:
    """Builds a small current.sqlite with the same tables and columns the importer reads."""

    def __init__(self, path):
        self.con = sqlite3.connect(path)
        for table, columns in rebuild.COLUMNS.items():
            self.con.execute(f'CREATE TABLE "{table}" ({", ".join(f"[{c}]" for c in columns)})')
        self.con.execute('CREATE TABLE meta (key TEXT, value TEXT)')
        self.con.execute('CREATE TABLE controls (section TEXT, subject TEXT, metric TEXT, value TEXT)')
        self.con.executemany('INSERT INTO meta VALUES (?, ?)', [('source_sha256', SHA), ('content_sha256', 'cd' * 32)])
        self.con.executemany('INSERT INTO controls VALUES (?, ?, ?, ?)', [('report', 'receipt', 'Total_equal', '5'), ('report', 'receipt', 'Total_mismatch', '0'), ('census', 'receipt', 'rows_selected', '5')])

    def row(self, table, **values):
        columns = rebuild.COLUMNS[table]
        unknown = set(values) - set(columns)
        assert not unknown, f'{table}: unknown columns {unknown}'
        values.setdefault('selected', 1)
        self.con.execute(f'INSERT INTO "{table}" ({", ".join(f"[{c}]" for c in columns)}) VALUES ({", ".join("?" * len(columns))})',
                         [values.get(c) for c in columns])

    def finish(self):
        self.con.commit()
        self.con.close()


def write_fixture(path):
    f = FixtureWriter(path)
    f.row('department', SID=SID['D1'], DeptCode='FUR', DeptName='Furniture', TaxCode=0, MarginPercent=40)
    f.row('vendor', SID=SID['V1'], VendCode='ACME', Company='Acme Furniture', FName='Ann', LName='Maker', Address1='1 Mill Rd', City='Troy', State='NY', ZIP='12180', Phone1='518-555-0100', Email='sales@acme.example', AccNum='WC-77', TermsNetDays=30, IsInActive=0)
    f.row('vendor', SID=SID['V2'], VendCode='OLDV', Company='Old Vendor', Email='WWW.NOT-AN-EMAIL.COM', TermsNetDays=0, IsInActive=1)
    f.row('customer', SID=SID['C1'], CustomerID='4000010001', Title='Ms', FName='Pat', LName='Buyer', IsCompany=0, Address1='2 Elm St', City='Springfield', State='NY', ZIP='62701', Phone1='518-555-0101', Phone2='518-555-0102', Phone3='518-555-0103', Email='pat@example.com', Comments='Prefers delivery', AcceptChecks=1, EmailNotify=1, ShipAddrName='Camp', ShipAddress='9 Lake Rd', ShipCity='Bolton', ShipState='NY', ShipZIP='12814')
    f.row('customer', SID=SID['C2'], CustomerID='4000010002', FName='Lee', LName='Owner', IsCompany=1, Company='Lakeside Inn', TaxArea='Exempt', AcceptChecks=0, EmailNotify=0)
    f.row('item', ItemSID=SID['I1'], ItemNO=1001, QBItemType=0, TaxCode=0, SerialFlag=0, PrintTag=1, NoCommission=0, TrackRewards=1, Desc1='Oak Chair', Desc2='Oak side chair', InventoryLongDesc__Desc2='Oak side chair with slat back', Attr='OAK', Size='18"', UPC='0001', ALU='A1', Manufacturer='Acme', UnitOfMeasure='Each', VendSID=SID['V1'], VendCode='ACME', DeptSID=SID['D1'], Cost='40.123456', LastCost='41.5', CompanyOHQty='5', TotO_O='3', CmpMin='1', MSRP='0', Price1='99.95', Price2='99.95', Price3='99.95', Price4='89.123', Price5='99.95', Price6='0', Weight='0', Length='0', Width='0', Height='0', Comments='Popular', LastRecvd='2026-01-05T00:00:00.000000')
    f.row('item', ItemSID=SID['I2'], ItemNO=1002, QBItemType=1, TaxCode=1, SerialFlag=1, PrintTag=0, NoCommission=1, TrackRewards=0, Desc1='Delivery', Desc2='Delivery fee', VendSID=SID['GONE'], DeptSID=SID['D1'], Cost='0', LastCost='0', CompanyOHQty='0', TotO_O='0', CmpMin='0', MSRP='0', Price1='10', Price2='10', Price3='10', Price4='10', Price5='10', Price6='0', Weight='0', Length='0', Width='0', Height='0')
    f.row('item_vendor', ItemSID=SID['I1'], VendorPos=0, VendSID=None, OrderCost='41.5')
    # ordinary sale with a return line, a deleted-item line, a split tender with change and a memo row.
    f.row('receipt', SID=SID['R1'], ReceiptNum=101, ReceiptType=0, ReceiptStatus=0, BillToSID=SID['C1'], Store=1, TenderType=9, Cashier='Morgan', Clerk='Alex', Comments='Thanks', TaxArea='Springfield', SubTotal='194.90', TaxAmount='13.64', TaxPercent='7', Total='208.54', ReceiptDate='2026-03-04', ReceiptTime='14:05:06.000000', ReceiptCustomer__CustSID=SID['C1'], ReceiptCustomer__IsCompany=0, ReceiptCustomer__CustTitle='Ms', ReceiptCustomer__CustFName='Pat', ReceiptCustomer__CustLName='Buyer')
    f.row('receipt_line', SID=SID['R1'], ItemPos=0, ItemSID=SID['I1'], Qty='2', Price='99.95', Cost='40.123456', TaxPercent='7', Clerk='Alex', ReceiptItemDesc__ItemNo=1001, ReceiptItemDesc__Desc1='Oak Chair', ReceiptItemDesc__Desc2='Oak side chair', ReceiptItemLongDesc__Desc2='Oak side chair with slat back')
    f.row('receipt_line', SID=SID['R1'], ItemPos=1, ItemSID=SID['DEAD'], Qty='1', Price='5', Cost='0', TaxPercent='7', Clerk='Alex', ReceiptItemDesc__ItemNo=0, ReceiptItemDesc__Desc1='Payment on account', ReceiptItemDesc__Desc2=None)
    f.row('receipt_line', SID=SID['R1'], ItemPos=2, ItemSID=SID['I2'], Qty='-1', Price='10', Cost='0', TaxPercent='7', Clerk='Alex', DocRefSID=SID['R3'], ReceiptItemDesc__ItemNo=1002, ReceiptItemDesc__Desc1='Delivery', ReceiptItemDesc__Desc2='Delivery fee')
    f.row('receipt_tender', SID=SID['R1'], TenderPos=0, TenderType=3, TenderUseInTotal=0, TenderAmount='150.00', TenderName='Visa', TenderNumber=None)
    f.row('receipt_tender', SID=SID['R1'], TenderPos=1, TenderType=1, TenderUseInTotal=0, TenderAmount='60.00', TenderName='Cash')
    f.row('receipt_tender', SID=SID['R1'], TenderPos=2, TenderType=1, TenderUseInTotal=0, TenderAmount='-1.46', TenderName='Cash')
    f.row('receipt_tender', SID=SID['R1'], TenderPos=3, TenderType=6, TenderUseInTotal=1, TenderAmount='-5.00', TenderName='Account')
    # deposit receipt for SO1.
    f.row('receipt', SID=SID['R2'], ReceiptNum=102, ReceiptType=2, ReceiptStatus=0, BillToSID=SID['C2'], SORefSID=SID['SO1'], SONum='7', Store=1, TenderType=2, Cashier='Morgan', Clerk='Morgan', SubTotal='0', TaxAmount='0', TaxPercent='7', Total='50.00', ReceiptDate='2026-03-01', ReceiptTime='09:00:00.000000', ReceiptCustomer__CustSID=SID['C2'], ReceiptCustomer__IsCompany=1, ReceiptCustomer__CustCompany='Lakeside Inn', ReceiptCustomer__CustFName='Lee', ReceiptCustomer__CustLName='Owner')
    f.row('receipt_tender', SID=SID['R2'], TenderPos=0, TenderType=2, TenderUseInTotal=0, TenderAmount='50.00', TenderName='Check', TenderNumber='4471')
    # Receipt reversals: reversed original and its reversal document.
    f.row('receipt', SID=SID['R3'], ReceiptNum=103, ReceiptType=0, ReceiptStatus=1, ReceiptRefSID=SID['R4'], BillToSID=SID['C1'], Store=1, TenderType=1, Cashier='Alex', Clerk='Alex', SubTotal='10', TaxAmount='0.70', TaxPercent='7', Total='10.70', ReceiptDate='2026-02-01', ReceiptTime='10:00:00.000000', ReceiptCustomer__CustSID=SID['C1'], ReceiptCustomer__CustFName='Pat', ReceiptCustomer__CustLName='Buyer')
    f.row('receipt_line', SID=SID['R3'], ItemPos=0, ItemSID=SID['I2'], Qty='1', Price='10', Cost='0', TaxPercent='7', Clerk='Alex', ReceiptItemDesc__ItemNo=1002, ReceiptItemDesc__Desc1='Delivery', ReceiptItemDesc__Desc2='Delivery fee')
    f.row('receipt_tender', SID=SID['R3'], TenderPos=0, TenderType=1, TenderUseInTotal=0, TenderAmount='10.70', TenderName='Cash')
    f.row('receipt', SID=SID['R4'], ReceiptNum=104, ReceiptType=1, ReceiptStatus=2, ReceiptRefSID=SID['R3'], BillToSID=SID['C1'], Store=1, TenderType=1, Cashier='Alex', Clerk='Alex', SubTotal='10', TaxAmount='0.70', TaxPercent='7', Total='10.70', ReceiptDate='2026-02-01', ReceiptTime='10:05:00.000000', ReceiptCustomer__CustSID=SID['C1'], ReceiptCustomer__CustFName='Pat', ReceiptCustomer__CustLName='Buyer')
    f.row('receipt_line', SID=SID['R4'], ItemPos=0, ItemSID=SID['I2'], Qty='1', Price='10', Cost='0', TaxPercent='7', Clerk='Alex', ReceiptItemDesc__ItemNo=1002, ReceiptItemDesc__Desc1='Delivery', ReceiptItemDesc__Desc2='Delivery fee')
    f.row('receipt_tender', SID=SID['R4'], TenderPos=0, TenderType=1, TenderUseInTotal=0, TenderAmount='-10.70', TenderName='Cash')
    # pickup sale that consumed the SO1 deposit (type-8 tender); tax-exempt customer; unresolved total annotation.
    f.row('receipt', SID=SID['R5'], ReceiptNum=105, ReceiptType=0, ReceiptStatus=0, BillToSID=SID['C2'], SORefSID=SID['SO1'], SONum='7', Store=1, TenderType=9, Cashier='Morgan', Clerk='Morgan', SubTotal='99.95', TaxAmount='0', TaxPercent='0', Total=None, ReceiptDate='2026-03-10', ReceiptTime='16:30:00.000000', ReceiptCustomer__CustSID=SID['C2'], ReceiptCustomer__IsCompany=1, ReceiptCustomer__CustCompany='Lakeside Inn')
    f.row('receipt_line', SID=SID['R5'], ItemPos=0, ItemSID=SID['I1'], Qty='1', Price='99.95', Cost='40.123456', TaxPercent='0', Clerk='Morgan', ReceiptItemDesc__ItemNo=1001, ReceiptItemDesc__Desc1='Oak Chair', ReceiptItemDesc__Desc2='Oak side chair')
    f.row('receipt_tender', SID=SID['R5'], TenderPos=0, TenderType=8, TenderUseInTotal=0, TenderAmount='50.00', TenderName=None)
    f.row('receipt_tender', SID=SID['R5'], TenderPos=1, TenderType=1, TenderUseInTotal=0, TenderAmount=None, TenderName='Cash')
    # Sales orders: SO1 open with a deposit, SO2 a superseded DataState-2 draft, SO3 closed with a non-numeric number.
    f.row('sales_order', SID=SID['SO1'], SONum='7', Status=0, DataState=0, BillToSID=SID['C2'], Store=1, Cashier='Morgan', Clerk='Morgan', Comments='Deliver Friday', TaxPercent='0', SubTotal='99.95', TaxAmount='0', Total='99.95', SODue='49.95', DepositTaken='50.00', DepositUsed='0', SODate='2026-03-01', SalesOrderCustomer__CustSID=SID['C2'], SalesOrderCustomer__IsCompany=1, SalesOrderCustomer__CustCompany='Lakeside Inn', SalesOrderCustomer__CustPONumber='LI-9', SalesOrderShipTo__ShipAddress='4 Shore Rd', SalesOrderShipTo__ShipCity='Bolton', SalesOrderShipTo__ShipState='NY', SalesOrderShipTo__ShipZIP='12814')
    f.row('sales_order_line', SID=SID['SO1'], ItemPos=0, ItemSID=SID['I1'], Qty='1', QtySent='0', Price='99.95', Cost='40.123456', TaxPercent='0', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair', SalesOrderItemDesc__Desc2='Oak side chair')
    f.row('sales_order', SID=SID['SO2'], SONum='7', Status=0, DataState=2, selected=0, BillToSID=SID['C2'], TaxPercent='0', SubTotal='99.95', TaxAmount='0', Total='99.95', DepositTaken='0', DepositUsed='0', SODate='2026-03-01')
    f.row('sales_order_line', SID=SID['SO2'], selected=0, ItemPos=0, ItemSID=SID['I1'], Qty='1', QtySent='0', Price='99.95', Cost='0', TaxPercent='0', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair')
    f.row('sales_order', SID=SID['SO3'], SONum='QUOTE', Status=1, DataState=0, BillToSID=SID['C1'], Store=1, Cashier='Alex', Clerk='Alex', TaxPercent='7', SubTotal='10', TaxAmount='0.70', Total='10.70', SODue='0', DepositTaken='10.70', DepositUsed='10.70', SODate='2009-05-05', SalesOrderCustomer__CustSID=SID['C1'], SalesOrderCustomer__CustFName='Pat', SalesOrderCustomer__CustLName='Buyer')
    f.row('sales_order_line', SID=SID['SO3'], ItemPos=0, ItemSID=SID['GONE'], Qty='1', QtySent='1', Price='10', Cost='0', TaxPercent='7', SalesOrderItemDesc__ItemNo=555, SalesOrderItemDesc__Desc1='Old Table', SalesOrderItemDesc__Desc2='Retired table')
    # SO4: QB order 5388 (10/9/2026) with a 5 % whole-order discount: QB's DiscAmount 215.99 is one cent under the
    # per-line half-up sum (121.00 + 26.50 + 68.50), so the largest line carries the leftover; tax on the net amounts.
    f.row('sales_order', SID=SID['SO4'], SONum='5', Status=0, DataState=0, BillToSID=SID['C1'], Store=1, Cashier='Morgan', Clerk='Morgan', TaxPercent='7', SubTotal='4319.80', DiscAmount='215.99', DiscPercent='5', TaxAmount='287.27', Total='4391.08', SODue='4391.08', DepositTaken='0', DepositUsed='0', SODate='2026-10-09', SalesOrderCustomer__CustSID=SID['C1'], SalesOrderCustomer__CustFName='Pat', SalesOrderCustomer__CustLName='Buyer')
    f.row('sales_order_line', SID=SID['SO4'], ItemPos=0, ItemSID=SID['I1'], Qty='2', QtySent='0', Price='1209.95', OrigPrice='1209.95', Cost='500', TaxPercent='7', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair')
    f.row('sales_order_line', SID=SID['SO4'], ItemPos=1, ItemSID=SID['I1'], Qty='1', QtySent='0', Price='529.95', OrigPrice='529.95', Cost='200', TaxPercent='7', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair')
    f.row('sales_order_line', SID=SID['SO4'], ItemPos=2, ItemSID=SID['I1'], Qty='1', QtySent='0', Price='1369.95', OrigPrice='1369.95', Cost='600', TaxPercent='7', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair')
    # Purchase orders: PO1 open (vendor by SID, one over-received line), PO2 vendor by code fallback, PO3 vendor unresolvable.
    f.row('purchase_order', SID=SID['PO1'], PONum='501', Status=0, SuppVendSID=SID['V1'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', Comments='Rush', Total='200.00', TermsNetDays=30, PODate='2026-02-10', POShipDate='2026-02-20', POTotDueDate='2026-03-20')
    f.row('purchase_order_line', SID=SID['PO1'], ItemPos=0, ItemSID=SID['I1'], Qty='4', QtyRcvd='1', Cost='40', PurchaseOrderItemDesc__ItemNo=1001, PurchaseOrderItemDesc__Desc1='Oak Chair', PurchaseOrderItemDesc__Desc2='Oak side chair')
    f.row('purchase_order_line', SID=SID['PO1'], ItemPos=1, ItemSID=SID['I2'], Qty='1', QtyRcvd='2', Cost='40', PurchaseOrderItemDesc__ItemNo=1002, PurchaseOrderItemDesc__Desc1='Delivery')
    f.row('purchase_order', SID=SID['PO2'], PONum='502', Status=1, SuppVendSID=SID['GONE'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', Total='40.00', TermsNetDays=0, PODate='2025-02-10', POShipDate='2025-02-20', POTotDueDate='2025-03-20')
    f.row('purchase_order_line', SID=SID['PO2'], ItemPos=0, ItemSID=SID['GONE'], Qty='1', QtyRcvd='1', Cost='40', PurchaseOrderItemDesc__ItemNo=555, PurchaseOrderItemDesc__Desc1='Old Table')
    f.row('purchase_order', SID=SID['PO3'], PONum='k', Status=1, SuppVendSID=SID['GONE'], SuppVendCode='NOPE', SuppVendCompany='Vanished Vendor', Clerk='Morgan', Total='0', TermsNetDays=0, PODate='2012-02-10', POShipDate='2012-02-20', POTotDueDate='2012-03-20')
    f.row('purchase_order_line', SID=SID['PO3'], ItemPos=0, ItemSID=SID['I2'], Qty='0', QtyRcvd='0', Cost='0', PurchaseOrderItemDesc__ItemNo=1002, PurchaseOrderItemDesc__Desc1='Delivery')
    # Vouchers: VO1 receives against PO1; VO2/VO3 are a reversal pair.
    f.row('voucher', SID=SID['VO1'], VoucherNum=901, VoucherType=0, VoucherStatus=0, SuppVendSID=SID['V1'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', InvoiceNumber='INV-1', Comments='Partial', Total='40.00', Freight='12.50', VoucherDate='2026-02-25', VoucherTime='11:15:00.000000', InvoiceDate='2026-02-24', InvoiceDueDate='2026-03-26')
    f.row('voucher_line', SID=SID['VO1'], ItemPos=0, ItemSID=SID['I1'], PORefSID=SID['PO1'], Qty='1', Cost='40', VoucherItemDesc__ItemNo=1001, VoucherItemDesc__Desc1='Oak Chair', VoucherItemDesc__Desc2='Oak side chair')
    f.row('voucher', SID=SID['VO2'], VoucherNum=902, VoucherType=0, VoucherStatus=1, DocRefSID=SID['VO3'], SuppVendSID=SID['V1'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', Total='40.00', Freight='0', VoucherDate='2026-02-26', VoucherTime='11:15:00.000000')
    f.row('voucher_line', SID=SID['VO2'], ItemPos=0, ItemSID=SID['I1'], PORefSID=None, Qty='1', Cost='40', VoucherItemDesc__ItemNo=1001, VoucherItemDesc__Desc1='Oak Chair')
    f.row('voucher', SID=SID['VO3'], VoucherNum=903, VoucherType=1, VoucherStatus=2, DocRefSID=SID['VO2'], SuppVendSID=SID['V1'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', Total='40.00', Freight='0', VoucherDate='2026-02-26', VoucherTime='11:20:00.000000')
    f.row('voucher_line', SID=SID['VO3'], ItemPos=0, ItemSID=SID['GONE'], PORefSID=SID['GONE'], Qty='1', Cost='40', VoucherItemDesc__ItemNo=555, VoucherItemDesc__Desc1='Old Table')
    f.finish()


def snapshot():
    """Every business row keyed by source identity, foreign keys replaced by the related row's source_id."""
    def customer(pk):
        return Customer.objects.get(pk=pk).source_id if pk else None
    rows = []
    rows += [('dept', d.source_id, d.code, d.name, str(d.tax_rate), str(d.margin)) for d in Department.objects.all()]
    rows += [('vendor', v.source_id, v.code, v.company, v.contact, v.email, v.terms, v.active) for v in Vendor.objects.all()]
    rows += [('customer', c.source_id, c.customer_number, c.full_name, c.company, c.track_as_company, c.tax_exempt, c.street, c.email) for c in Customer.objects.all()]
    rows += [('address', a.customer.source_id, a.address, a.is_default) for a in CustomerAddress.objects.all()]
    rows += [('item', i.source_id, i.sku, i.name, i.description, str(i.price), str(i.price_4), str(i.on_hand), str(i.imported_on_order), str(i.imported_last_received), i.primary_vendor.source_id if i.primary_vendor else None, i.department.source_id if i.department else None) for i in Item.objects.all()]
    for d in Document.objects.all():
        rows.append(('doc', d.kind, d.source_id, d.source_number, d.status, d.receipt_type, customer(d.customer_id), d.customer_name, str(d.subtotal), str(d.tax), str(d.total), str(d.tendered), d.payment_method, d.cashier, d.clerk, d.store, str(d.deposit_taken), str(d.deposit_used), str(d.completed_at), str(d.created_at), d.notes, d.shipping_address, d.customer_po))
        rows += [('line', l.source_id, l.position, l.item.source_id if l.item else None, l.sku, l.item_name, l.description, str(l.quantity), str(l.price), str(l.tax_rate), str(l.unit_cost)) for l in d.lines.all()]
        rows += [('tender', t.source_id, t.position, t.method, str(t.amount), t.tender_name, t.reference, t.use_in_total) for t in d.tenders.all()]
    for p in PurchaseDocument.objects.all():
        rows.append(('pdoc', p.kind, p.source_id, p.source_number, p.status, p.vendor.source_id if p.vendor else None, p.vendor_name, p.source_po.source_id if p.source_po else None, str(p.total), p.terms, str(p.ship_date), str(p.payment_due), p.invoice_number, str(p.invoice_date), str(p.freight), str(p.posted_at), str(p.created_at), p.clerk, p.notes))
        rows += [('pline', l.source_id, l.position, l.item.source_id if l.item else None, l.sku, l.item_name, l.description, str(l.quantity), str(l.cost), str(l.received), str(l.imported_received), l.source_line.source_id if l.source_line else None) for l in p.lines.all()]
    return sorted(rows, key=repr)


class RebuildTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        import tempfile
        cls.dir = tempfile.mkdtemp(prefix='rebuild-')
        cls.path = str(Path(cls.dir) / 'current.sqlite')
        write_fixture(cls.path)

    def run_rebuild(self, **options):
        options.setdefault('commit', True)
        return rebuild.rebuild_from_backup(self.path, **options)

    def test_counts_and_mappings(self):
        result = self.run_rebuild()
        counts = result['counts']
        self.assertEqual({k: counts[k] for k in ['departments', 'vendors', 'customers', 'customer_addresses', 'items', 'receipts', 'receipt_lines', 'tenders', 'orders', 'order_lines', 'purchase_orders', 'purchase_order_lines', 'vouchers', 'voucher_lines']},
                         dict(departments=1, vendors=2, customers=2, customer_addresses=1, items=2, receipts=5, receipt_lines=6, tenders=9, orders=3, order_lines=5, purchase_orders=3, purchase_order_lines=4, vouchers=3, voucher_lines=3))
        self.assertEqual(result['source_sha256'], SHA)
        # Masters.
        dept = Department.objects.get()
        self.assertEqual((dept.code, dept.name, dept.tax_rate, dept.margin, dept.source_id), ('FUR', 'Furniture', 7, 40, SID['D1']))
        acme = Vendor.objects.get(source_id=SID['V1'])
        self.assertEqual((acme.code, acme.contact, acme.email, acme.terms, acme.account_number, acme.active, acme.postal_code), ('ACME', 'Ann Maker', 'sales@acme.example', '30 days', 'WC-77', True, '12180'))
        old = Vendor.objects.get(source_id=SID['V2'])
        self.assertEqual((old.email, old.terms, old.active), ('', '', False))
        pat = Customer.objects.get(source_id=SID['C1'])
        self.assertEqual((pat.customer_number, pat.title, pat.first_name, pat.last_name, pat.full_name, pat.track_as_company, pat.tax_exempt, pat.accept_checks, pat.email_opt_in, pat.mobile, pat.alternate_phone, pat.notes),
                         ('4000010001', 'Ms', 'Pat', 'Buyer', 'Ms Pat Buyer', False, False, True, True, '518-555-0102', '518-555-0103', 'Prefers delivery'))
        self.assertEqual(pat.source_data['SID'], SID['C1'])
        self.assertEqual(pat.addresses.get().address, 'Camp\n9 Lake Rd\nBolton NY 12814')
        inn = Customer.objects.get(source_id=SID['C2'])
        self.assertEqual((inn.company, inn.track_as_company, inn.tax_exempt, inn.tax_location, inn.accept_checks, inn.display_name), ('Lakeside Inn', True, True, 'Exempt', False, 'Lakeside Inn'))
        chair = Item.objects.get(source_id=SID['I1'])
        self.assertEqual((chair.sku, chair.name, chair.description, chair.wood, chair.size, chair.item_type, chair.tax_rate, chair.tax_category), ('1001', 'Oak Chair', 'Oak side chair with slat back', 'OAK', '18"', 'inventory', 7, 'Taxable Sales'))
        self.assertEqual((chair.price, chair.price_4, chair.order_cost, chair.average_cost, chair.on_hand, chair.imported_on_order, chair.reorder_point, chair.imported_last_received),
                         (Decimal('99.95'), Decimal('89.12'), Decimal('41.50'), Decimal('40.12'), 5, 3, 1, date(2026, 1, 5)))  # QB fractions truncated
        self.assertEqual((chair.primary_vendor, chair.vendor, chair.department, chair.print_tags, chair.earn_commission, chair.earn_rewards, chair.use_serial_numbers, chair.comments), (acme, 'ACME', dept, True, True, True, False, 'Popular'))
        fee = Item.objects.get(source_id=SID['I2'])
        self.assertEqual((fee.item_type, fee.tax_rate, fee.tax_category, fee.primary_vendor, fee.imported_last_received, fee.earn_commission, fee.use_serial_numbers), ('noninventory', 0, 'Non', None, None, False, True))
        self.assertEqual(ItemVendor.objects.count(), 0)
        self.assertEqual(counts['item_vendors_skipped'], 1)
        # Receipts.
        r1 = Document.objects.get(source_id=SID['R1'])
        self.assertEqual((r1.kind, r1.status, r1.receipt_type, r1.source_number, r1.number, r1.customer, r1.customer_name, r1.was_completed), ('sale', 'completed', 0, 101, '101', pat, 'Ms Pat Buyer', True))
        self.assertEqual((r1.subtotal, r1.tax, r1.total, r1.tendered, r1.payment_method, r1.tax_exempt, r1.cashier, r1.clerk, r1.store, r1.notes), (Decimal('194.90'), Decimal('13.64'), Decimal('208.54'), Decimal('208.54'), 'split', False, 'Morgan', 'Alex', 1, 'Thanks'))
        local = timezone.localtime(r1.completed_at)
        self.assertEqual((local.year, local.month, local.day, local.hour, local.minute, local.second, local.tzinfo.key), (2026, 3, 4, 14, 5, 6, 'America/New_York'))
        self.assertEqual(r1.created_at, r1.completed_at)
        lines = list(r1.lines.all())
        self.assertEqual([(l.position, l.item, l.sku, l.item_name, l.description, l.quantity, l.price, l.tax_rate, l.unit_cost, l.source_id) for l in lines], [
            (0, chair, '1001', 'Oak Chair', 'Oak side chair with slat back', 2, Decimal('99.95'), 7, Decimal('40.12'), f'{SID["R1"]}:0'),
            (1, None, '', 'Payment on account', 'Payment on account', 1, 5, 7, 0, f'{SID["R1"]}:1'),
            (2, fee, '1002', 'Delivery', 'Delivery fee', -1, 10, 7, 0, f'{SID["R1"]}:2')])
        self.assertEqual([(t.position, t.method, t.amount, t.tender_name, t.reference, t.use_in_total) for t in r1.tenders.all()],
                         [(0, 'credit', Decimal('150.00'), 'Visa', '', False), (1, 'cash', Decimal('60.00'), 'Cash', '', False), (2, 'cash', Decimal('-1.46'), 'Cash', '', False), (3, 'account', Decimal('-5.00'), 'Account', '', True)])
        self.assertEqual(r1.payment_label, 'Visa, Cash')
        self.assertEqual(r1.change, Decimal('0.00'))
        deposit = Document.objects.get(source_id=SID['R2'])
        self.assertEqual((deposit.kind, deposit.receipt_type, deposit.type_label, deposit.status, deposit.lines.count(), deposit.total, deposit.payment_method, deposit.customer_name), ('sale', 2, 'Deposit', 'completed', 0, 50, 'check', 'Lakeside Inn'))
        self.assertEqual(deposit.tenders.get().reference, '4471')
        original, reversal = Document.objects.get(source_id=SID['R3']), Document.objects.get(source_id=SID['R4'])
        self.assertEqual((original.status, original.receipt_type, reversal.status, reversal.receipt_type, reversal.type_label), ('cancelled', 0, 'cancelled', 1, 'Sales reversal'))
        pickup = Document.objects.get(source_id=SID['R5'])
        self.assertEqual((pickup.total, pickup.tendered, pickup.tax_exempt, pickup.payment_method), (None, None, True, 'split'))
        self.assertEqual([(t.method, t.tender_name, t.amount) for t in pickup.tenders.all()], [('other', 'Deposit', 50), ('cash', 'Cash', None)])
        self.assertEqual(counts['receipt_totals_unresolved'], 1)
        self.assertEqual(counts['tender_amounts_unresolved'], 1)
        self.assertEqual(counts['receipt_lines_without_item'], 1)
        self.assertEqual(counts['receipt_subtotal_mismatches'], 0)
        self.assertEqual(Document.objects.filter(kind='sale').count(), 5)
        self.assertFalse(StockMovement.objects.exists())
        # Orders.
        so1 = Document.objects.get(source_id=SID['SO1'])
        self.assertEqual((so1.kind, so1.status, so1.number, so1.customer, so1.customer_name, so1.total, so1.deposit_taken, so1.deposit_used, so1.paid, so1.balance_due, so1.customer_po, so1.notes, so1.tax_exempt, so1.cashier),
                         ('order', 'open', '7', inn, 'Lakeside Inn', Decimal('99.95'), 50, 0, 50, Decimal('49.95'), 'LI-9', 'Deliver Friday', True, 'Morgan'))
        self.assertEqual(so1.shipping_address, '4 Shore Rd\nBolton NY 12814')
        self.assertEqual(timezone.localtime(so1.created_at).date(), date(2026, 3, 1))
        self.assertEqual(so1.lines.get().item, chair)
        self.assertFalse(Document.objects.filter(source_id=SID['SO2']).exists())
        so3 = Document.objects.get(source_id=SID['SO3'])
        self.assertEqual((so3.status, so3.source_number, so3.number, so3.applied_payments, so3.unused_payments), ('completed', None, '', Decimal('10.70'), 0))
        self.assertIn('QUOTE', so3.notes)
        self.assertIsNone(so3.lines.get().item)
        self.assertEqual(so3.lines.get().sku, '555')
        self.assertFalse(OrderPayment.objects.exists())
        self.assertEqual(counts['orders_without_number'], 1)
        # the whole-order discount comes over with its percent, and the lines get QB's spread of it (leftover cent
        # on the largest line), so Subtotal - Discount + Tax = Total and the net line amounts feed the reports.
        so4 = Document.objects.get(source_id=SID['SO4'])
        self.assertEqual((so4.discount, so4.discount_percent, so4.discount_reason, so4.subtotal, so4.tax, so4.total), (Decimal('215.99'), Decimal('5'), '', Decimal('4319.80'), Decimal('287.27'), Decimal('4391.08')))
        self.assertEqual([l.receipt_discount for l in so4.lines.all()], [Decimal('120.99'), Decimal('26.50'), Decimal('68.50')])
        self.assertEqual(sum(l.net_amount for l in so4.lines.all()), so4.subtotal - so4.discount)
        self.assertEqual((so1.discount, so1.lines.get().receipt_discount), (0, 0))
        self.assertEqual((counts['orders_with_discount'], counts['order_lines_with_discount_share']), (1, 3))
        # Purchase orders.
        po1 = PurchaseDocument.objects.get(source_id=SID['PO1'])
        self.assertEqual((po1.kind, po1.status, po1.number, po1.vendor, po1.vendor_name, po1.total, po1.terms, po1.ship_date, po1.payment_due, po1.notes, po1.clerk), ('po', 'open', 'PO-501', acme, 'Acme Furniture', 200, '30 days', date(2026, 2, 20), date(2026, 3, 20), 'Rush', 'Morgan'))
        self.assertEqual(timezone.localtime(po1.created_at).date(), date(2026, 2, 10))
        self.assertEqual([(l.item, l.sku, l.quantity, l.cost, l.received, l.imported_received) for l in po1.lines.all()], [(chair, '1001', 4, 40, 1, 1), (fee, '1002', 1, 40, 1, 1)])
        self.assertEqual(counts['purchase_received_overflow'], 1)
        po2 = PurchaseDocument.objects.get(source_id=SID['PO2'])
        self.assertEqual((po2.status, po2.vendor, po2.lines.get().item, po2.lines.get().item_name), ('closed', acme, None, 'Old Table'))
        po3 = PurchaseDocument.objects.get(source_id=SID['PO3'])
        self.assertEqual((po3.vendor, po3.vendor_name, po3.number, po3.lines.get().quantity), (None, 'Vanished Vendor', 'PO-k', 0))
        self.assertEqual(counts['purchase_orders_without_vendor'], 1)
        # Vouchers.
        vo1 = PurchaseDocument.objects.get(source_id=SID['VO1'])
        self.assertEqual((vo1.kind, vo1.status, vo1.number, vo1.vendor, vo1.source_po, vo1.invoice_number, vo1.invoice_date, vo1.payment_due, vo1.freight, vo1.total, vo1.notes), ('voucher', 'posted', 'RV-901', acme, po1, 'INV-1', date(2026, 2, 24), date(2026, 3, 26), Decimal('12.50'), 40, 'Partial'))
        self.assertEqual(timezone.localtime(vo1.posted_at).hour, 11)
        self.assertEqual(vo1.lines.get().source_line, po1.lines.get(position=0))
        self.assertEqual(PurchaseDocument.objects.get(source_id=SID['VO2']).status, 'cancelled')
        self.assertEqual(PurchaseDocument.objects.get(source_id=SID['VO3']).status, 'cancelled')
        self.assertEqual(counts['vouchers_without_po'], 2)
        self.assertEqual(counts['voucher_lines_without_po_line'], 2)
        self.assertFalse(ReceivingMovement.objects.exists())

    def test_negative_and_zero_lines_are_stored_and_native_validation_still_applies(self):
        self.run_rebuild()
        self.assertEqual(Line.objects.filter(quantity__lt=0).count(), 1)
        self.assertEqual(PurchaseLine.objects.filter(quantity=0).count(), 1)
        doc = Document.objects.create(kind='sale', status='held')
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Line.objects.create(document=doc, position=0, description='Native', quantity=-1, price=1)

    def test_qb_return_link_is_stored_and_backfilled(self):
        """Receipt item DocRefSID (QB's return -> original receipt link) lands on Line.returned_from_receipt, both on
        a rebuild and through `backfill_return_links` (same Loader.return_link), so the Accept Return cap can count
        QB-era returns."""
        result = self.run_rebuild()
        original = Document.objects.get(source_id=SID['R3'])
        linked = Line.objects.get(source_id=f"{SID['R1']}:2")
        self.assertEqual(linked.returned_from_receipt_id, original.pk)
        self.assertEqual(Line.objects.filter(returned_from_receipt__isnull=False).count(), 1)
        self.assertEqual(result['counts']['receipt_lines_return_linked'], 1)
        Line.objects.update(returned_from_receipt=None)
        out = io.StringIO()
        call_command('backfill_return_links', self.path, stdout=out)  # dry run
        self.assertIn('"updated": 1', out.getvalue())
        self.assertFalse(Line.objects.filter(returned_from_receipt__isnull=False).exists())
        call_command('backfill_return_links', self.path, '--commit', stdout=io.StringIO())
        self.assertEqual(Line.objects.get(pk=linked.pk).returned_from_receipt_id, original.pk)

    def test_order_discount_is_backfilled_like_the_import(self):
        """databases loaded before the importer read sales_order.DiscAmount get the order discount, percent and
        line shares through `backfill_order_discounts` (same order_discount_fields / spread_order_discounts)."""
        self.run_rebuild()
        so4 = Document.objects.get(source_id=SID['SO4'])
        Document.objects.filter(pk=so4.pk).update(discount=0, discount_percent=0)
        so4.lines.update(receipt_discount=0)
        out = io.StringIO()
        call_command('backfill_order_discounts', self.path, stdout=out)  # dry run
        report = json.loads(out.getvalue())
        self.assertEqual((report['committed'], report['source_orders_with_discount'], report['matched'], report['orders_updated'], report['line_shares_updated'], report['orders_not_netting_to_subtotal_minus_discount']), (False, 1, 1, 1, 3, []))
        self.assertEqual(Document.objects.get(pk=so4.pk).discount, 0)
        call_command('backfill_order_discounts', self.path, '--commit', stdout=io.StringIO())
        so4.refresh_from_db()
        self.assertEqual((so4.discount, so4.discount_percent, so4.total), (Decimal('215.99'), Decimal('5'), Decimal('4391.08')))
        self.assertEqual([l.receipt_discount for l in so4.lines.all()], [Decimal('120.99'), Decimal('26.50'), Decimal('68.50')])
        out = io.StringIO()
        call_command('backfill_order_discounts', self.path, '--commit', stdout=out)  # idempotent
        self.assertEqual((json.loads(out.getvalue())['orders_updated'], json.loads(out.getvalue())['line_shares_updated']), (0, 0))

    def test_idempotent_reload_yields_identical_rows(self):
        self.run_rebuild()
        first = snapshot()
        self.run_rebuild()
        self.assertEqual(snapshot(), first)
        self.assertEqual(Document.objects.count(), 8)

    def test_wipe_scope_keeps_printers_users_and_resets_numbering(self):
        user = User.objects.create_user('tester', password='x')
        printer_count = Printer.objects.count()
        self.run_rebuild()
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertEqual(Printer.objects.count(), printer_count)
        vendor = Vendor.objects.get(source_id=SID['V1'])
        item = Item.objects.get(source_id=SID['I1'])
        Item.objects.create(sku='9999', name='Native', description='Native item', price=1, on_hand=1, average_cost=1, tax_rate=7, primary_vendor=vendor)
        from purchasing.item_numbers import next_item_number
        from django.db import transaction
        with transaction.atomic():
            self.assertEqual(next_item_number(), '10000')
        StockMovement.objects.create(document=Document.objects.get(source_id=SID['R1']), item=item, quantity=-1)
        InventoryAdjustment.objects.create(item=item, old_quantity=1, new_quantity=2)
        self.run_rebuild()
        self.assertFalse(Item.objects.filter(sku='9999').exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(InventoryAdjustment.objects.exists())
        self.assertEqual(ItemNumberCounter.objects.filter(last_number__gt=0).count(), 0)

    def test_refuses_native_completed_documents_unless_discarded(self):
        self.run_rebuild()
        native = Document.objects.create(kind='sale', status='completed', was_completed=True)
        with self.assertRaisesRegex(rebuild.RebuildError, 'discard-test-data'):
            self.run_rebuild()
        self.assertTrue(Document.objects.filter(pk=native.pk).exists())
        self.run_rebuild(discard_test_data=True)
        self.assertFalse(Document.objects.filter(pk=native.pk).exists())

    def test_dry_run_changes_nothing(self):
        Document.objects.create(kind='sale', status='held')
        result = self.run_rebuild(commit=False)
        self.assertEqual(result['counts']['receipts'], 5)
        self.assertFalse(result['committed'])
        self.assertEqual(Document.objects.count(), 1)
        self.assertFalse(Customer.objects.exists())

    def test_validation_refusals(self):
        bad = str(Path(self.dir) / 'bad.sqlite')
        write_fixture(bad)
        con = sqlite3.connect(bad)
        con.execute("UPDATE controls SET value='2' WHERE metric='Total_mismatch'")
        con.commit(); con.close()
        with self.assertRaisesRegex(rebuild.RebuildError, 'mismatch'):
            rebuild.rebuild_from_backup(bad, commit=True)
        con = sqlite3.connect(bad)
        con.execute("UPDATE controls SET value='0'")
        con.execute("DELETE FROM meta WHERE key='source_sha256'")
        con.commit(); con.close()
        with self.assertRaisesRegex(rebuild.RebuildError, 'source_sha256'):
            rebuild.rebuild_from_backup(bad, commit=True)
        with self.assertRaises(rebuild.RebuildError):
            rebuild.rebuild_from_backup(str(Path(self.dir) / 'missing.sqlite'), commit=True)
        self.assertFalse(Customer.objects.exists())

    def test_management_command_reports_counts_and_fails_loudly(self):
        out = io.StringIO()
        call_command('rebuild_from_backup', self.path, commit=True, discard_test_data=True, stdout=out)
        self.assertIn('"receipts": 5', out.getvalue())
        self.assertIn('committed', out.getvalue())
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', str(Path(self.dir) / 'missing.sqlite'), commit=True, stdout=io.StringIO())

    def test_imported_documents_render_and_are_read_only(self):
        self.run_rebuild()
        r1 = Document.objects.get(source_id=SID['R1'])
        page = self.client.get(f'/pos/sale/{r1.token}/')
        self.assertContains(page, 'Imported from QuickBooks POS (101)')
        self.assertContains(page, 'Visa: $150.00')
        self.assertTrue(page.context['readonly'])
        history = self.client.get('/pos/history/?status=all')
        self.assertContains(history, '>101<')
        self.assertContains(history, 'Deposit')
        self.assertEqual(self.client.get('/pos/history/?q=101').context['page'].paginator.count, 1)
        so1 = Document.objects.get(source_id=SID['SO1'])
        orders = self.client.get('/pos/orders/')
        self.assertContains(orders, '>7</a>')
        # an OPEN order from QuickBooks is workable (payments, pickups, cancel, edits) with QB's deposit counted;
        # a closed one stays read-only history.
        page = self.client.get(f'/pos/order/{so1.token}/')
        self.assertContains(page, 'Deposit taken in QuickBooks POS $50.00')
        self.assertContains(page, 'Payments and Pickup')
        self.assertFalse(page.context['readonly'])
        so3 = Document.objects.get(source_id=SID['SO3'])
        page = self.client.get(f'/pos/order/{so3.token}/')
        # Closed QB order (review finding 7): the locked "Deposits and Payments" summary with QB's deposit.
        self.assertContains(page, 'Deposits and Payments')
        self.assertContains(page, 'Deposit taken in QuickBooks POS $10.70')
        self.assertTrue(page.context['readonly'])
        po1 = PurchaseDocument.objects.get(source_id=SID['PO1'])
        self.assertContains(self.client.get('/purchasing/orders/'), 'PO-501')
        detail = self.client.get(f'/purchasing/po/{po1.token}/')
        self.assertContains(detail, 'Imported from QuickBooks POS (PO-501')
        self.assertTrue(detail.context['readonly'])
        self.assertContains(self.client.get('/purchasing/history/?period=all'), 'RV-901')
        self.assertEqual(self.client.get('/purchasing/orders/?q=501').context['page'].paginator.count, 1)
        items = self.client.get('/purchasing/items/?q=Oak')
        self.assertEqual(items.context['page'][0].on_order, 3)
        self.assertEqual(items.context['page'][0].last_received, date(2026, 1, 5))
        self.assertEqual(self.client.get('/customers/').status_code, 200)
        from django.http import Http404
        from .print_documents import pdf, load
        doc, kind, title = load('sale', r1.token)
        self.assertTrue(pdf(doc, kind, title).startswith(b'%PDF'))
        # Editing an imported document is refused, both for the sale editor and order workflows.
        import json
        response = self.client.post(f'/pos/sale/{r1.token}/', json.dumps(dict(token=str(r1.token), version=r1.version, action='cancel')), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('read-only', response.json()['error'])
        response = self.client.post(f'/pos/order/{so3.token}/payment/', dict(version=so3.version, token='00000000-0000-0000-0000-000000000001', amount='5', method='cash'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('read-only', response.url)
        self.assertFalse(OrderPayment.objects.exists())
        response = self.client.post(f'/pos/order/{so1.token}/payment/', dict(version=so1.version, token='00000000-0000-0000-0000-000000000001', amount='5', method='cash'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('error', response.url)
        so1.refresh_from_db()
        self.assertEqual((so1.paid, so1.balance_due), (Decimal('55.00'), Decimal('44.95')))

    def test_native_receiving_against_imported_po_counts_prior_receipts(self):
        self.run_rebuild()
        from purchasing.services import save_purchase
        from purchasing.tests import header, rows
        po1 = PurchaseDocument.objects.get(source_id=SID['PO1'])
        chair = Item.objects.get(source_id=SID['I1'])
        line = po1.lines.get(position=0)
        with self.assertRaisesRegex(Exception, 'exceeds'):
            save_purchase(header(po1.vendor), rows(chair, '4', '40', source_line=line.pk), 'voucher', 'post', po1)
        voucher = save_purchase(header(po1.vendor), rows(chair, '3', '40', source_line=line.pk), 'voucher', 'post', po1)
        line.refresh_from_db(); po1.refresh_from_db(); chair.refresh_from_db()
        self.assertEqual((line.received, line.imported_received, po1.status, chair.on_hand), (4, 1, 'closed', 8))
        self.assertEqual(voucher.number, f'RV-{voucher.pk:06d}')
        items = self.client.get('/purchasing/items/?q=Oak')
        self.assertEqual(items.context['page'][0].on_order, 0)  # PO-501 is now fully received (closed); QB's old item total isn't read
        self.assertEqual(items.context['page'][0].last_received, timezone.localtime(voucher.posted_at).date())
