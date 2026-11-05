# SPDX-License-Identifier: AGPL-3.0-or-later
"""History cutoff at import: `rebuild_from_backup(..., history_from=date)` brings over sales
information from the cutoff on; all customers; balances carried = min(stored, Account charges since the cutoff);
credits only with activity in the window; pre-cutoff orders dropped (open or closed); open POs kept whatever their
date; held receipts kept whatever their date; returns of pre-cutoff receipts keep no link. No option = unchanged."""
import io
import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command, CommandError
from django.test import TestCase

from customers.models import Customer
from purchasing.models import PurchaseDocument, PurchaseLine
from reports.tax_ledger import open_charges
from . import rebuild
from .models import Document, Line, Tender
from .test_held import write_held
from .test_rebuild import FixtureWriter, SHA, snapshot

CUT = date(2021, 9, 1)
SID = {name: f'{index:016x}' for index, name in enumerate(
    ['D1', 'V1', 'C1', 'C2', 'C3', 'C4', 'C5', 'I1', 'R_OLD', 'R_IN1', 'R_IN2', 'R_RET', 'R_C4', 'R_DEP_OLD', 'R_DEP_IN', 'R_POA',
     'SO_OLD_OPEN', 'SO_OLD_CLOSED', 'SO_IN', 'PO_OLD_OPEN', 'PO_OLD_CLOSED', 'PO_OLD_RECV', 'PO_IN', 'VO_OLD', 'VO_WIN', 'VO_OLD2', 'H_OLD', 'C6', 'R_C6', 'C7', 'R_C7_OLD', 'R_C7_IN', 'C8', 'R_C8', 'SO_DISC'], start=1)}
HISTORY, BALANCES = date(2016, 9, 1), date(2021, 9, 1)   # the separate dates of the balance cutoff tests


def receipt(f, key, number, when, customer, total, tenders, lines, receipt_type=0, status=0, **extra):
    total_d = Decimal(total)
    tax = (total_d - total_d / Decimal('1.07')).quantize(Decimal('0.01')) if receipt_type == 0 else Decimal('0.00')
    f.row('receipt', SID=SID[key], ReceiptNum=number, ReceiptType=receipt_type, ReceiptStatus=status, BillToSID=SID[customer], Store=1, TaxArea='Springfield',
          Cashier='Morgan', Clerk='Morgan', SubTotal=str(total_d - tax), TaxAmount=str(tax), TaxPercent='7', Total=total, ReceiptDate=when, ReceiptTime='10:00:00.000000',
          ReceiptCustomer__CustSID=SID[customer], ReceiptCustomer__CustFName='Cust', ReceiptCustomer__CustLName=customer, **extra)
    for pos, (kind, amount, use_in_total) in enumerate(tenders):
        f.row('receipt_tender', SID=SID[key], TenderPos=pos, TenderType=kind, TenderUseInTotal=use_in_total, TenderAmount=amount, TenderName=None)
    for pos, line in enumerate(lines):
        f.row('receipt_line', SID=SID[key], ItemPos=pos, ItemSID=SID['I1'], Qty=line.get('qty', '1'), Price=line.get('price', '100'), Cost='40', TaxPercent='7',
              DocRefSID=line.get('ref'), ReceiptItemDesc__ItemNo=1001, ReceiptItemDesc__Desc1=line.get('name', 'Oak Chair'))


def write_fixture(path, balance_cases=False):
    """The cutoff dataset; `balance_cases` adds the separate balance cutoff cases: C6 (a credit whose only
    receipt is inside the 10-year history but before the balance date), C7 (owed 80: 40 put on the account before the
    balance date and 60 on/after it -> the aged rule carries 60), C8 (owes 200 from a charge dated exactly on the balance
    date -> carried in full) and SO_DISC, a discounted open order inside the window (shares must survive)."""
    f = FixtureWriter(path)
    f.row('department', SID=SID['D1'], DeptCode='FUR', DeptName='Furniture', TaxCode=0, MarginPercent=40)
    f.row('vendor', SID=SID['V1'], VendCode='ACME', Company='Acme Furniture', TermsNetDays=30, IsInActive=0)
    # C1 owes 500 (past due 450): 400 charged before the cutoff, 300 after -> carries 300 (partly). C2 owes 100 with
    # 250 charged in the window -> carries 100 in full. C3 is the house account: owes 1000, no activity -> 0.
    # C4 is owed 50 (credit) and shopped in the window -> keeps -50. C5 is owed 20, no activity -> 0.
    for key, number, balance, past_due in [('C1', 1, '500', '450'), ('C2', 2, '100', '0'), ('C3', 3, '1000', '1000'), ('C4', 4, '-50', '0'), ('C5', 5, '-20', '0')] + ([('C6', 6, '-30', '0'), ('C7', 7, '-80', '0'), ('C8', 8, '200', '0')] if balance_cases else []):
        f.row('customer', SID=SID[key], CustomerID=f'400001000{number}', FName='Cust', LName=key, IsCompany=0, UseAccCharge=1, CreditUsed=balance, Credit90Overdue=past_due)
    f.row('item', ItemSID=SID['I1'], ItemNO=1001, QBItemType=0, TaxCode=0, SerialFlag=0, PrintTag=1, NoCommission=0, TrackRewards=0, Desc1='Oak Chair', Desc2='Oak side chair',
          VendSID=SID['V1'], VendCode='ACME', DeptSID=SID['D1'], Cost='40', LastCost='40', CompanyOHQty='5', TotO_O='0', CmpMin='1', MSRP='0', Price1='100', Price2='100', Price3='100', Price4='100', Price5='100', Price6='0')
    receipt(f, 'R_OLD', 101, '2020-05-01', 'C1', '400', [(6, '400', 0)], [{'price': '400'}])
    receipt(f, 'R_IN1', 102, '2022-03-04', 'C1', '300', [(6, '300', 0)], [{'price': '300'}])
    receipt(f, 'R_IN2', 103, '2022-03-05', 'C2', '250', [(6, '250', 0)], [{'price': '250'}])
    # A payment on account in the window: the memo Account row (use_in_total) is not a charge.
    receipt(f, 'R_POA', 104, '2022-04-01', 'C2', '150', [(6, '-150', 1), (3, '150', 0)], [{'price': '150', 'name': 'Payment On Account'}])
    # Return (in the window) of the pre-cutoff receipt R_OLD: the return comes over, the link is absent.
    receipt(f, 'R_RET', 105, '2023-01-10', 'C1', '-100', [(1, '-100', 0)], [{'qty': '-1', 'price': '100', 'ref': SID['R_OLD']}])
    receipt(f, 'R_C4', 106, '2024-06-01', 'C4', '107', [(1, '107', 0)], [{'price': '100'}])
    if balance_cases:
        receipt(f, 'R_C6', 109, '2018-06-01', 'C6', '107', [(1, '107', 0)], [{'price': '100'}])
        # C7: money put on the account twice (an Account tender below zero): a return refunded to the account for 40
        # before the balance date, and a 60 cash payment on account (memo Account row, a shop's real shape) on/after
        # it; the aged rule keeps the newest 60 (the old 40 is spent first).
        receipt(f, 'R_C7_OLD', 110, '2019-03-01', 'C7', '-40', [(6, '-40', 0)], [{'qty': '-1', 'price': '40'}])
        receipt(f, 'R_C7_IN', 111, '2022-07-01', 'C7', '60', [(6, '-60', 1), (3, '60', 0)], [{'price': '60', 'name': 'Payment On Account'}])
        # C8: the charge sits exactly on the balance date (>=, not >).
        receipt(f, 'R_C8', 112, BALANCES.isoformat(), 'C8', '200', [(6, '200', 0)], [{'price': '200'}])
        # SO_DISC: open order in the window with a 10 % whole-order discount, two lines 100 + 50.
        f.row('sales_order', SID=SID['SO_DISC'], SONum='10', Status=0, DataState=0, BillToSID=SID['C2'], Store=1, Cashier='Morgan', Clerk='Morgan', TaxPercent='7', SubTotal='150', DiscAmount='15', DiscPercent='10', TaxAmount='9.45', Total='144.45',
              SODue='144.45', DepositTaken='0', DepositUsed='0', SODate='2023-03-15', SalesOrderCustomer__CustSID=SID['C2'], SalesOrderCustomer__CustFName='Cust', SalesOrderCustomer__CustLName='C2')
        for pos, price in enumerate(('100', '50')):
            f.row('sales_order_line', SID=SID['SO_DISC'], ItemPos=pos, ItemSID=SID['I1'], Qty='1', QtySent='0', Price=price, Cost='40', TaxPercent='7', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair')
    # Deposits: one in the window on a pre-cutoff open order (order dropped, deposit kept, unlinked); one on an in-window order.
    receipt(f, 'R_DEP_OLD', 107, '2021-10-01', 'C2', '50', [(2, '50', 0)], [], receipt_type=2, SORefSID=SID['SO_OLD_OPEN'], SONum='7')
    receipt(f, 'R_DEP_IN', 108, '2022-05-01', 'C2', '75', [(2, '75', 0)], [], receipt_type=2, SORefSID=SID['SO_IN'], SONum='9')
    for key, number, status, when in [('SO_OLD_OPEN', '7', 0, '2021-08-18'), ('SO_OLD_CLOSED', '8', 1, '2020-01-01'), ('SO_IN', '9', 0, '2022-04-20')]:
        f.row('sales_order', SID=SID[key], SONum=number, Status=status, DataState=0, BillToSID=SID['C2'], Store=1, Cashier='Morgan', Clerk='Morgan', TaxPercent='7', SubTotal='100', TaxAmount='7', Total='107',
              SODue='57', DepositTaken='50', DepositUsed='0', SODate=when, SalesOrderCustomer__CustSID=SID['C2'], SalesOrderCustomer__CustFName='Cust', SalesOrderCustomer__CustLName='C2')
        f.row('sales_order_line', SID=SID[key], ItemPos=0, ItemSID=SID['I1'], Qty='1', QtySent='0', Price='100', Cost='40', TaxPercent='7', SalesOrderItemDesc__ItemNo=1001, SalesOrderItemDesc__Desc1='Oak Chair')
    # POs: open from 2015 (kept), closed 2019 (dropped), closed 8/2021 but received 9/10/2021 (kept for its voucher), 2023 (kept).
    for key, number, status, when in [('PO_OLD_OPEN', '501', 0, '2015-02-10'), ('PO_OLD_CLOSED', '502', 1, '2019-02-10'), ('PO_OLD_RECV', '503', 1, '2021-08-20'), ('PO_IN', '504', 0, '2023-02-10')]:
        f.row('purchase_order', SID=SID[key], PONum=number, Status=status, SuppVendSID=SID['V1'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', Total='40', TermsNetDays=30, PODate=when, POShipDate=when, POTotDueDate=when)
        f.row('purchase_order_line', SID=SID[key], ItemPos=0, ItemSID=SID['I1'], Qty='4', QtyRcvd='1' if status == 0 else '4', Cost='40', PurchaseOrderItemDesc__ItemNo=1001, PurchaseOrderItemDesc__Desc1='Oak Chair')
    # Vouchers: 2015 against the open PO (dropped), 9/10/2021 against PO_OLD_RECV (kept, linked), 2019 against the closed PO (dropped).
    for key, number, when, po in [('VO_OLD', '901', '2015-03-01', 'PO_OLD_OPEN'), ('VO_WIN', '902', '2021-09-10', 'PO_OLD_RECV'), ('VO_OLD2', '903', '2019-03-01', 'PO_OLD_CLOSED')]:
        f.row('voucher', SID=SID[key], VoucherNum=number, VoucherType=0, VoucherStatus=0, SuppVendSID=SID['V1'], SuppVendCode='ACME', SuppVendCompany='Acme Furniture', Clerk='Morgan', Total='40', Freight='0', VoucherDate=when, VoucherTime='11:00:00.000000')
        f.row('voucher_line', SID=SID[key], ItemPos=0, ItemSID=SID['I1'], PORefSID=SID[po], Qty='1', Cost='40', VoucherItemDesc__ItemNo=1001, VoucherItemDesc__Desc1='Oak Chair')
    f.finish()
    with mock.patch('sales.test_held.SHA', SHA):   # same sealed backup as current.sqlite
        write_held(Path(path).with_name('held.sqlite'), [
        dict(SID=SID['H_OLD'], ReceiptNum=99, ReceiptType=0, ReceiptStatus=0, Store=1, Cashier='Morgan', SubTotal='100', TaxAmount='7', TaxPercent='7', Total='107', ReceiptDate='2017-10-27', ReceiptTime='14:40:29.000000'),
    ], [
        dict(SID=SID['H_OLD'], ItemPos=0, ItemSID=SID['I1'], Qty='1', Price='100', TaxPercent='7', ReceiptItemDesc__ItemNo=1001, ReceiptItemDesc__Desc1='Oak Chair'),
    ])


class HistoryCutoffTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dir = tempfile.mkdtemp(prefix='cutoff-')
        cls.path = str(Path(cls.dir) / 'current.sqlite')
        write_fixture(cls.path)

    def load(self, **options):
        options.setdefault('commit', True)
        return rebuild.rebuild_from_backup(self.path, **options)

    def customer(self, key):
        return Customer.objects.get(source_id=SID[key])

    def test_no_option_loads_the_full_history_unchanged(self):
        result = self.load()
        counts = result['counts']
        self.assertIsNone(result['history_from'])
        self.assertEqual(Document.objects.filter(kind='sale', source_id__isnull=False).exclude(status='held').count(), 8)
        self.assertEqual(Document.objects.filter(kind='order').count(), 3)
        self.assertEqual(PurchaseDocument.objects.filter(kind='po').count(), 4)
        self.assertEqual(PurchaseDocument.objects.filter(kind='voucher').count(), 3)
        self.assertEqual([self.customer(k).imported_account_balance for k in ('C1', 'C2', 'C3', 'C4', 'C5')], [Decimal('500'), Decimal('100'), Decimal('1000'), Decimal('-50'), Decimal('-20')])
        self.assertEqual(self.customer('C1').imported_past_due, Decimal('450'))
        self.assertFalse([k for k in counts if 'cutoff' in k or k.startswith(('balances_', 'credits_'))], 'no cutoff counts without the option')
        full = snapshot()
        self.load()
        self.assertEqual(snapshot(), full)

    def test_cutoff_drops_pre_cutoff_sales_and_carries_balances_by_the_rules(self):
        result = self.load(history_from=CUT)
        counts = result['counts']
        self.assertEqual(result['history_from'], '2021-09-01')
        # Receipts: R_OLD dropped with its line and tender; everything in the window kept.
        kept = set(Document.objects.filter(kind='sale', source_id__isnull=False).exclude(status='held').values_list('source_number', flat=True))
        self.assertEqual(kept, {102, 103, 104, 105, 106, 107, 108})
        self.assertEqual((counts['receipts_before_cutoff'], counts['receipt_lines_before_cutoff'], counts['tenders_before_cutoff']), (1, 1, 1))
        self.assertFalse(Line.objects.filter(source_id__startswith=SID['R_OLD']).exists())
        self.assertFalse(Tender.objects.filter(source_id__startswith=SID['R_OLD']).exists())
        # Return of the pre-cutoff receipt: kept, link absent, counted (not as "missing").
        ret = Line.objects.get(document__source_id=SID['R_RET'])
        self.assertIsNone(ret.returned_from_receipt_id)
        self.assertEqual(counts['receipt_lines_return_link_before_cutoff'], 1)
        self.assertEqual(counts.get('receipt_lines_return_link_missing', 0), 0)
        # Balances: min(stored, charges in the window); the memo Account row of a payment on account is not a charge.
        self.assertEqual(self.customer('C1').imported_account_balance, Decimal('300'))     # partly: 500 stored, 300 charged since
        self.assertEqual(self.customer('C1').imported_past_due, Decimal('300'))            # capped at the carried balance
        self.assertEqual(self.customer('C2').imported_account_balance, Decimal('100'))     # in full: 250 charged since
        self.assertEqual(self.customer('C3').imported_account_balance, Decimal('0'))       # house account: no activity
        self.assertEqual(self.customer('C3').imported_past_due, Decimal('0'))
        self.assertEqual(self.customer('C4').imported_account_balance, Decimal('-50'))     # credit with activity: kept
        self.assertEqual(self.customer('C5').imported_account_balance, Decimal('0'))       # credit without activity: dropped
        self.assertEqual(self.customer('C3').source_data['CreditUsed'], '1000')            # QB's figure still readable
        self.assertEqual({k: counts[k] for k in ('balances_carried', 'balances_carried_in_full', 'balances_carried_partly', 'balances_dropped', 'credits_carried', 'credits_dropped')},
                         dict(balances_carried=2, balances_carried_in_full=1, balances_carried_partly=1, balances_dropped=1, credits_carried=1, credits_dropped=1))
        self.assertEqual((counts['balances_carried_total'], counts['balances_dropped_total']), (Decimal('400'), Decimal('1200')))
        self.assertEqual((counts['credits_carried_total'], counts['credits_dropped_total']), (Decimal('-50'), Decimal('-20')))
        self.assertEqual((counts['balances_largest_carried'], counts['balances_largest_dropped']), (Decimal('300'), Decimal('1000')))
        # Orders: both pre-cutoff orders gone (open and closed); the in-window one kept with its deposit linked.
        self.assertEqual(list(Document.objects.filter(kind='order').values_list('source_number', flat=True)), [9])
        self.assertEqual((counts['open_orders_before_cutoff'], counts['closed_orders_before_cutoff'], counts['order_lines_before_cutoff']), (1, 1, 2))
        dep_old, dep_in = Document.objects.get(source_id=SID['R_DEP_OLD']), Document.objects.get(source_id=SID['R_DEP_IN'])
        self.assertIsNone(dep_old.deposit_order_id)
        self.assertEqual(dep_in.deposit_order.source_number, 9)
        self.assertEqual((counts['deposits_on_orders_before_cutoff'], counts['deposits_on_orders_before_cutoff_total'], counts['deposits_linked']), (1, Decimal('50.00'), 1))
        self.assertEqual(counts['deposits_without_order'], 0)
        # POs: open 2015 kept; closed 2019 dropped; closed 8/2021 kept because received 9/10/2021; 2023 kept.
        self.assertEqual(set(PurchaseDocument.objects.filter(kind='po').values_list('source_number', flat=True)), {'501', '503', '504'})
        self.assertEqual((counts['closed_purchase_orders_before_cutoff'], counts['purchase_orders_kept_for_window_voucher'], counts['open_purchase_orders_kept_before_cutoff'], counts['purchase_order_lines_before_cutoff']), (1, 1, 1, 1))
        old_open = PurchaseDocument.objects.get(source_number='501')
        self.assertEqual([(l.received, l.imported_received) for l in old_open.lines.all()], [(Decimal('1'), Decimal('1'))])   # from QB's PO line, no voucher needed
        # Vouchers: only the 9/10/2021 one survives, linked to its PO; the 2015 one on the kept open PO is counted.
        vouchers = PurchaseDocument.objects.filter(kind='voucher')
        self.assertEqual([(v.source_number, v.source_po.source_number) for v in vouchers], [('902', '503')])
        self.assertEqual((counts['vouchers_before_cutoff'], counts['vouchers_before_cutoff_on_kept_po'], counts['voucher_lines_before_cutoff'], counts['vouchers_without_po']), (2, 1, 2, 0))
        self.assertTrue(PurchaseLine.objects.get(document__source_number='902').source_line_id)
        # Held receipts come over whatever their date.
        self.assertEqual(Document.objects.filter(status='held', source_id=SID['H_OLD']).count(), 1)
        self.assertEqual(counts['held_receipts'], 1)
        # Items and customers all came over.
        self.assertEqual(Customer.objects.count(), 5)
        self.assertEqual(counts['customers'], 5)

    def test_ledger_owed_matches_the_carried_balance(self):
        """The tax ledger's view of opening balances stays consistent: what a customer shows as owed per receipt adds
        up to the carried balance, and the house account shows nothing owed."""
        self.load(history_from=CUT)
        for key, expected in [('C1', Decimal('300')), ('C2', Decimal('100')), ('C3', Decimal('0')), ('C4', Decimal('0')), ('C5', Decimal('0'))]:
            customer = self.customer(key)
            owed = sum((row[2] for row in open_charges(customer)), Decimal('0'))
            self.assertEqual(owed, expected, key)
            self.assertEqual(max(customer.account_balance, Decimal('0')), expected, key)
        # C1's single open charge is the in-window receipt, owed in full (the old 400 and the window's 100 return never meet).
        self.assertEqual([(row[0][0], row[2]) for row in open_charges(self.customer('C1'))], [('sale', Decimal('300'))])

    def test_dry_run_with_cutoff_changes_nothing(self):
        self.load()
        before = snapshot()
        result = self.load(history_from=CUT, commit=False)
        self.assertFalse(result['committed'])
        self.assertEqual(result['counts']['receipts_before_cutoff'], 1)
        self.assertEqual(snapshot(), before)

    def test_history_cutoff_rule_rounds_to_a_tax_quarter_start(self):
        self.assertEqual(rebuild.history_cutoff(5, date(2026, 10, 15)), date(2021, 9, 1))
        self.assertEqual(rebuild.history_cutoff(5, date(2026, 12, 1)), date(2021, 12, 1))
        self.assertEqual(rebuild.history_cutoff(5, date(2027, 2, 10)), date(2021, 12, 1))
        self.assertEqual(rebuild.history_cutoff(5, date(2027, 3, 1)), date(2022, 3, 1))
        self.assertEqual(rebuild.history_cutoff(3, date(2028, 2, 29)), date(2024, 12, 1))
        with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 10, 15)):
            self.assertEqual(rebuild.history_cutoff(), date(2021, 9, 1))

    def test_management_command_options(self):
        out, err = io.StringIO(), io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-from', '2021-09-01', stdout=out, stderr=err)
        result = json.loads(out.getvalue())
        self.assertEqual(result['history_from'], '2021-09-01')
        self.assertEqual(result['counts']['receipts_before_cutoff'], 1)
        self.assertIn('history cutoff: sales documents from 9/1/2021 on', err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-years', '5', '--cutover-date', '2026-10-15', stdout=out, stderr=err)
        self.assertEqual(json.loads(out.getvalue())['history_from'], '2021-09-01')
        out, err = io.StringIO(), io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-from', '2022-03-01', '--history-years', '5', stdout=out, stderr=err)
        self.assertEqual(json.loads(out.getvalue())['history_from'], '2022-03-01')
        self.assertIn('--history-years ignored', err.getvalue())
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--history-from', 'yesterday', stdout=io.StringIO(), stderr=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--cutover-date', '2026-10-15', stdout=io.StringIO(), stderr=io.StringIO())
        out = io.StringIO()
        call_command('rebuild_from_backup', self.path, stdout=out, stderr=io.StringIO())
        self.assertIsNone(json.loads(out.getvalue())['history_from'])

    def test_follow_up_backfills_find_nothing_to_change_after_a_cutoff_load(self):
        """The rebuild already stores return links, unit prices, recorded amounts and discount reasons; the follow-up
        commands must be no-ops on a cutoff load (and never resurrect dropped lines)."""
        self.load(history_from=CUT)
        held = str(Path(self.path).with_name('held.sqlite'))
        out = io.StringIO()
        call_command('backfill_return_links', self.path, held, '--commit', stdout=out)
        result = json.loads(out.getvalue())['counts']
        self.assertEqual(result['current.sqlite']['updated'], 0)
        out = io.StringIO()
        call_command('backfill_unit_prices', self.path, held, '--commit', stdout=out)
        self.assertEqual({k: v['updated'] for k, v in json.loads(out.getvalue())['counts'].items()}, {'current.sqlite:receipt_line': 0, 'current.sqlite:sales_order_line': 0, 'current.sqlite:receipt': 0, 'held.sqlite:receipt_line': 0, 'held.sqlite:receipt': 0})
        out = io.StringIO()
        call_command('backfill_recorded_amounts', self.path, held, '--commit', stdout=out)
        self.assertEqual({k: v['updated'] for k, v in json.loads(out.getvalue())['counts'].items()}, {'current.sqlite:receipt_line': 0, 'current.sqlite:sales_order_line': 0, 'current.sqlite:purchase_order_line': 0, 'current.sqlite:voucher_line': 0, 'held.sqlite:receipt_line': 0})
        out = io.StringIO()
        call_command('backfill_discount_reasons', self.path, '--commit', stdout=out)
        self.assertEqual(json.loads(out.getvalue())['updated'], 0)
        self.assertFalse(Line.objects.filter(source_id__startswith=SID['R_OLD']).exists())


class BalanceCutoffTests(TestCase):
    """Separate balance cutoff: history from 9/1/2016 keeps every receipt of the fixture, the
    balance date 9/1/2021 forgives what is still due on older charges. C1 owes 500 on charges of 400 (2020) and 300
    (2022): payments FIFO settle 200 of the old charge, its remaining 200 is forgiven, the 300 carries. C2 owes 100
    with 250 charged since -> 100. C3 (the house account, owes 1000, nothing charged since) -> 0. Credits: C4 shopped
    in 2024 (kept by every rule), C5 never shopped, C6 shopped in 2018 (inside the history, before the balance date)."""
    @classmethod
    def setUpTestData(cls):
        cls.dir = tempfile.mkdtemp(prefix='balances-')
        cls.path = str(Path(cls.dir) / 'current.sqlite')
        write_fixture(cls.path, balance_cases=True)

    def load(self, **options):
        options.setdefault('commit', True)
        return rebuild.rebuild_from_backup(self.path, **options)

    def balances(self):
        return {key: Customer.objects.get(source_id=SID[key]).imported_account_balance for key in ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8')}

    def test_balance_date_forgives_old_charges_but_keeps_the_receipts(self):
        result = self.load(history_from=HISTORY, balances_from=BALANCES)
        counts = result['counts']
        self.assertEqual((result['history_from'], result['balances_from'], result['credits']), ('2016-09-01', '2021-09-01', 'history'))
        # Every receipt is inside the 10-year history: nothing dropped, the 2020 charge is still there to read.
        self.assertEqual(counts['receipts_before_cutoff'], 0)
        self.assertTrue(Document.objects.filter(source_id=SID['R_OLD'], status='completed').exists())
        self.assertEqual(Line.objects.get(document__source_id=SID['R_RET']).returned_from_receipt.source_id, SID['R_OLD'])
        self.assertEqual(self.balances(), {'C1': Decimal('300'), 'C2': Decimal('100'), 'C3': Decimal('0'), 'C4': Decimal('-50'), 'C5': Decimal('0'), 'C6': Decimal('-30'), 'C7': Decimal('-80'), 'C8': Decimal('200')})
        self.assertEqual(Customer.objects.get(source_id=SID['C1']).imported_past_due, Decimal('300'))
        self.assertEqual(Customer.objects.get(source_id=SID['C3']).source_data['CreditUsed'], '1000')
        self.assertEqual({k: counts[k] for k in ('balances_carried', 'balances_carried_in_full', 'balances_carried_partly', 'balances_dropped', 'credits_carried', 'credits_dropped')},
                         dict(balances_carried=3, balances_carried_in_full=2, balances_carried_partly=1, balances_dropped=1, credits_carried=3, credits_dropped=1))
        self.assertEqual((counts['balances_carried_total'], counts['balances_dropped_total'], counts['balances_largest_dropped']), (Decimal('600'), Decimal('1200'), Decimal('1000')))
        self.assertEqual((counts['credits_carried_total'], counts['credits_dropped_total']), (Decimal('-160'), Decimal('-20')))

    def test_ledger_shows_only_the_carried_charges_as_owed(self):
        """Open Charges on the customer screen cap at the carried balance, oldest first, so the forgiven 2020 charge
        never shows as owed even though its receipt is loaded."""
        self.load(history_from=HISTORY, balances_from=BALANCES)
        c1 = Customer.objects.get(source_id=SID['C1'])
        self.assertEqual([(row[0][0], Document.objects.get(pk=row[0][1]).source_number, row[2]) for row in open_charges(c1)], [('sale', 102, Decimal('300'))])
        for key, expected in [('C2', Decimal('100')), ('C3', Decimal('0')), ('C4', Decimal('0')), ('C6', Decimal('0'))]:
            self.assertEqual(sum((row[2] for row in open_charges(Customer.objects.get(source_id=SID[key]))), Decimal('0')), expected, key)

    def test_credit_rules(self):
        credits = lambda: {k: v for k, v in self.balances().items() if k in ('C4', 'C5', 'C6', 'C7')}
        self.load(history_from=HISTORY, balances_from=BALANCES, credits='all')
        self.assertEqual(credits(), {'C4': Decimal('-50'), 'C5': Decimal('-20'), 'C6': Decimal('-30'), 'C7': Decimal('-80')})
        # 'balances' ages the credit itself: C4's old credit is
        # dropped even though C4 paid cash for something in the window (a prior review); C7 keeps only the
        # 60 put on the account on/after the balance date.
        result = self.load(history_from=HISTORY, balances_from=BALANCES, credits='balances')
        self.assertEqual(credits(), {'C4': Decimal('0'), 'C5': Decimal('0'), 'C6': Decimal('0'), 'C7': Decimal('-60')})
        counts = result['counts']
        self.assertEqual((counts['credits_carried'], counts['credits_carried_partly'], counts['credits_dropped']), (1, 1, 3))
        self.assertEqual((counts['credits_carried_total'], counts['credits_dropped_total']), (Decimal('-60'), Decimal('-120')))
        # The debts never depend on the credit rule; C8's charge exactly on the balance date counts (>=).
        self.assertEqual({k: v for k, v in self.balances().items() if k in ('C1', 'C2', 'C3', 'C8')}, {'C1': Decimal('300'), 'C2': Decimal('100'), 'C3': Decimal('0'), 'C8': Decimal('200')})

    def test_charge_exactly_on_the_balance_date_and_discounted_order_survive(self):
        result = self.load(history_from=HISTORY, balances_from=BALANCES, credits='balances')
        c8 = Customer.objects.get(source_id=SID['C8'])
        self.assertEqual(c8.imported_account_balance, Decimal('200'))
        self.assertEqual([(Document.objects.get(pk=row[0][1]).source_number, row[2]) for row in open_charges(c8)], [(112, Decimal('200'))])
        # the discounted in-window order keeps its header discount and line shares through a cutoff load.
        order = Document.objects.get(kind='order', source_number=10)
        self.assertEqual((order.discount, order.discount_percent, order.subtotal, order.tax, order.total), (Decimal('15.00'), Decimal('10.00'), Decimal('150.00'), Decimal('9.45'), Decimal('144.45')))
        self.assertEqual(sorted(order.lines.values_list('receipt_discount', flat=True)), [Decimal('5.00'), Decimal('10.00')])
        self.assertEqual(order.subtotal - order.discount + order.tax, order.total)
        self.assertEqual(result['counts']['orders_with_discount'], 1)

    def test_balance_date_alone_keeps_the_full_history(self):
        result = self.load(balances_from=BALANCES)
        self.assertIsNone(result['history_from'])
        self.assertEqual(result['balances_from'], '2021-09-01')
        self.assertEqual(Document.objects.filter(kind='sale', source_id__isnull=False).exclude(status='held').count(), 12)
        self.assertEqual(Document.objects.filter(kind='order').count(), 4)
        self.assertEqual(self.balances(), {'C1': Decimal('300'), 'C2': Decimal('100'), 'C3': Decimal('0'), 'C4': Decimal('-50'), 'C5': Decimal('-20'), 'C6': Decimal('-30'), 'C7': Decimal('-80'), 'C8': Decimal('200')})   # credits: all, without a history cutoff

    def test_history_date_alone_is_unchanged(self):
        """The old single-date behaviour: one date drives both rules."""
        result = self.load(history_from=CUT)
        self.assertEqual((result['history_from'], result['balances_from']), ('2021-09-01', '2021-09-01'))
        self.assertEqual(self.balances(), {'C1': Decimal('300'), 'C2': Decimal('100'), 'C3': Decimal('0'), 'C4': Decimal('-50'), 'C5': Decimal('0'), 'C6': Decimal('0'), 'C7': Decimal('-80'), 'C8': Decimal('200')})
        self.assertEqual(result['counts']['receipts_before_cutoff'], 3)   # R_OLD, R_C6 and R_C7_OLD

    def test_balance_date_before_the_history_date_is_refused(self):
        with self.assertRaises(rebuild.RebuildError):
            self.load(history_from=BALANCES, balances_from=HISTORY)
        with self.assertRaises(rebuild.RebuildError):
            self.load(history_from=HISTORY, balances_from=BALANCES, credits='some')

    def test_management_command_options(self):
        out, err = io.StringIO(), io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-from', '2016-09-01', '--balances-from', '2021-09-01', '--credits', 'all', stdout=out, stderr=err)
        result = json.loads(out.getvalue())
        self.assertEqual((result['history_from'], result['balances_from'], result['credits']), ('2016-09-01', '2021-09-01', 'all'))
        self.assertIn('balance cutoff: what is still due on Account charges before 9/1/2021 is forgiven', err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-years', '10', '--balance-years', '3', '--cutover-date', '2026-10-20', '--credits', 'balances', stdout=out, stderr=err)
        result = json.loads(out.getvalue())
        self.assertEqual((result['history_from'], result['balances_from'], result['credits']), ('2016-09-01', '2023-09-01', 'balances'))
        self.assertIn('--balance-years 3 from 2026-10-20: balances from 2023-09-01', err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-from', '2016-09-01', stdout=out, stderr=err)
        self.assertEqual(json.loads(out.getvalue())['balances_from'], '2016-09-01')   # one date drives both without the option
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--history-from', '2021-09-01', '--balances-from', '2016-09-01', stdout=io.StringIO(), stderr=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--credits', 'all', stdout=io.StringIO(), stderr=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--balances-from', 'soon', stdout=io.StringIO(), stderr=io.StringIO())
        # A supplied 0 is refused, not treated as "not given" (it would silently fall back to the history date).
        for args in (['--history-from', '2016-09-01', '--balance-years', '0', '--credits', 'balances'], ['--history-years', '0'], ['--history-from', '2016-09-01', '--balance-years', '-1', '--credits', 'balances']):
            with self.assertRaises(CommandError, msg=args):
                call_command('rebuild_from_backup', self.path, *args, stdout=io.StringIO(), stderr=io.StringIO())
        # A balance cutoff without an explicit credit rule is refused (the `history` default is an in-between answer).
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--history-from', '2016-09-01', '--balances-from', '2023-09-01', stdout=io.StringIO(), stderr=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('rebuild_from_backup', self.path, '--balance-years', '3', '--cutover-date', '2026-10-20', stdout=io.StringIO(), stderr=io.StringIO())
        out = io.StringIO()
        call_command('rebuild_from_backup', self.path, '--history-from', '2016-09-01', '--balances-from', '2023-09-01', '--credits', 'balances', stdout=out, stderr=io.StringIO())
        self.assertEqual(json.loads(out.getvalue())['credits'], 'balances')
