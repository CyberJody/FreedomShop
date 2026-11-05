# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuild the app's business data from a clean QB POS dataset (current.sqlite built in Stage 1).

Contract (working/research/20260927/STAGE3-MAPPING.md):
- validates meta.source_sha256, zero report mismatches in `controls`, and refuses to discard native completed
  documents unless discard_test_data=True;
- wipes business tables (Tender, Line, StockMovement, OrderPayment, Document, PurchaseLine, ReceivingMovement,
  PurchaseDocument, ItemVendor, InventoryAdjustment, Item, CustomerAddress, Customer, Vendor, Department) and keeps
  Printer, accounts, sessions, furniture-set tags (re-linked by item number) and the Items Trash (re-applied by
  item number: trashed items come back inactive with their reloaded QB stock zeroed again);
- loads parents then children with bulk_create inside one transaction, mapping SIDs to primary keys in memory;
- idempotent: a second run wipes and reloads the same rows.

No StockMovement / ReceivingMovement rows are created: Item.on_hand is the QB on-hand at backup time.

History cutoff: with `history_from` (a date) the load brings over sales information from that day on
only. The cutoff is a rule — five years back from the cutover, rounded down to a sales-tax quarter start
(`history_cutoff`); an explicit date always wins. All customers and items still come over. Rules:
- receipts (with lines and tenders) dated before the cutoff are dropped; a return of a pre-cutoff receipt keeps the
  return and simply has no link (counted);
- held receipts all come over whatever their date (the store uses them as set tags);
- sales orders dated before the cutoff are dropped, open or closed; a deposit receipt in
  the window that paid toward a dropped order stays as a deposit with no order link (counted, with its money);
- closed purchase orders dated before the cutoff are dropped unless a voucher in the window received against them
  (then the PO was really closed inside the window and the voucher keeps its "From PO" link); open POs are kept
  whatever their date — their received quantities come from QB's PO lines, not from vouchers;
- vouchers dated before the cutoff are dropped (receiving history), also those of kept open POs (counted);
- account balances: carried = min(stored QB balance, Account charges on or after the cutoff) — FIFO means what is
  still owed is the newest charges, so nothing older than the window carries (the dead house account drops to 0);
  past due is capped at the carried balance; a credit balance (store owes the customer) carries only when the
  customer has a completed receipt in the window. `Customer.source_data['CreditUsed']` still holds QB's figure.
Without `history_from` the load is the full history, exactly as before.

Balance cutoff ("if the customer has a balance due on their account that's more than three
years old, we are forgiving that balance"): `balances_from` (a date) separates the balance rule from the history
rule. Payments are applied FIFO (oldest charge first), so what a customer still owes sits on the newest charges;
the amount due on charges dated before `balances_from` is forgiven and the due on newer charges carries over:
carried = min(stored QB balance, Account charges on or after `balances_from`). `balances_from` may not be earlier
than `history_from` (every carried dollar must be traceable to a loaded receipt); without it the history date
drives both, as before. Credits (money owed TO the customer) follow the `credits` rule: 'history' (kept when the
customer has a completed receipt on/after the history date, all of them without a history cutoff), 'all' (every
credit kept), or 'balances' ("We are not keeping any store credits that are older than three years" —
the credit itself is aged like a debt: carried = -min(stored credit, money put on the account by Account tenders
below zero — payments on account, refunds to the account — on receipts dated on/after the balance date), the oldest
credit used up first).
"""
import sqlite3
import time
from pathlib import Path
from datetime import date, datetime, time as dtime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from customers.models import Customer, CustomerAddress
from purchasing.models import Department, ItemVendor, PurchaseDocument, PurchaseLine, ReceivingMovement, Vendor
from .models import GiftCertificate, GiftRedemption, Document, InventoryAdjustment, Item, ItemNumberCounter, Line, OrderPayment, OrderPaymentChange, ReceiptOverride, StockMovement, Tender, line_amount, qb_line_amount, unit_price

BATCH = 2000
LOCAL_TZ = ZoneInfo('America/New_York')
TENDER_METHODS = {1: 'cash', 2: 'check', 3: 'credit', 5: 'gift', 6: 'account', 7: 'gift', 8: 'other'}
TENDER_NAMES = {1: 'Cash', 2: 'Check', 3: 'Credit card', 5: 'Gift card', 6: 'Account', 7: 'Gift', 8: 'Deposit'}
CREDIT_RULES = ('history', 'all', 'balances')   # which credit balances (store owes the customer) carry; see the module docstring

# Columns read from each current.sqlite table (a subset of the source columns; names are the QB POS names).
COLUMNS = {
    'department': ['selected', 'SID', 'DeptCode', 'DeptName', 'TaxCode', 'MarginPercent', 'Comments'],
    'vendor': ['selected', 'SID', 'VendCode', 'Company', 'FName', 'LName', 'Address1', 'Address2', 'City', 'State', 'ZIP', 'Phone1', 'Phone2', 'Email', 'AccNum', 'TermsNetDays', 'IsInActive'],
    'customer': ['selected', 'SID', 'CustomerID', 'Title', 'FName', 'LName', 'IsCompany', 'Company', 'Address1', 'Address2', 'City', 'State', 'ZIP', 'Phone1', 'Phone2', 'Phone3', 'Phone4', 'Email', 'Comments', 'TaxArea', 'Type', 'AcceptChecks', 'EmailNotify', 'ShipAddrName', 'ShipCompany', 'ShipFullName', 'ShipAddress', 'ShipAddress2', 'ShipCity', 'ShipState', 'ShipZIP',
                 'AR', 'UseAccCharge', 'CreditLimit', 'CreditUsed', 'Credit30Overdue', 'Credit60Overdue', 'Credit90Overdue', 'LastSale'],
    'item': ['selected', 'ItemSID', 'ItemNO', 'QBItemType', 'TaxCode', 'SerialFlag', 'PrintTag', 'NoCommission', 'TrackRewards', 'Desc1', 'Desc2', 'InventoryLongDesc__Desc2', 'Attr', 'Size', 'UPC', 'ALU', 'Manufacturer', 'UnitOfMeasure', 'VendSID', 'VendCode', 'DeptSID', 'Cost', 'LastCost', 'CompanyOHQty', 'TotO_O', 'CmpMin', 'MSRP', 'Price1', 'Price2', 'Price3', 'Price4', 'Price5', 'Price6', 'Weight', 'Length', 'Width', 'Height', 'Comments', 'LastRecvd'],
    'item_vendor': ['selected', 'ItemSID', 'VendorPos', 'VendSID', 'OrderCost'],
    'receipt': ['selected', 'SID', 'ReceiptNum', 'ReceiptType', 'ReceiptStatus', 'Workstation', 'BillToSID', 'ReceiptRefSID', 'SORefSID', 'SONum', 'Store', 'TenderType', 'TaxArea', 'Cashier', 'Clerk', 'Comments', 'SubTotal', 'DiscAmount', 'DiscPercent', 'DiscountLevel', 'TaxAmount', 'TaxPercent', 'Total', 'ReceiptDate', 'ReceiptTime',
                'ReceiptCustomer__CustSID', 'ReceiptCustomer__IsCompany', 'ReceiptCustomer__CustCompany', 'ReceiptCustomer__CustTitle', 'ReceiptCustomer__CustFName', 'ReceiptCustomer__CustLName', 'ReceiptCustomer__CustPONumber',
                'ReceiptShipTo__ShipAddrName', 'ReceiptShipTo__ShipCompany', 'ReceiptShipTo__ShipFullName', 'ReceiptShipTo__ShipAddress', 'ReceiptShipTo__ShipAddress2', 'ReceiptShipTo__ShipCity', 'ReceiptShipTo__ShipState', 'ReceiptShipTo__ShipZIP'],
    'receipt_line': ['selected', 'SID', 'ItemPos', 'ItemSID', 'DocRefSID', 'Clerk', 'Qty', 'Price', 'OrigPrice', 'DiscountLevel', 'DiscountPercent', 'RealPrice', 'Cost', 'TaxPercent', 'ReceiptItemDesc__ItemNo', 'ReceiptItemDesc__Desc1', 'ReceiptItemDesc__Desc2', 'ReceiptItemLongDesc__Desc2'],
    'receipt_tender': ['selected', 'SID', 'TenderPos', 'TenderType', 'TenderUseInTotal', 'TenderAmount', 'TenderNumber', 'TenderName'],
    'sales_order': ['selected', 'SID', 'SONum', 'Status', 'DataState', 'BillToSID', 'Store', 'Cashier', 'Clerk', 'Comments', 'SubTotal', 'DiscAmount', 'DiscPercent', 'DiscountLevel', 'TaxAmount', 'TaxPercent', 'Total', 'SODue', 'DepositTaken', 'DepositUsed', 'SODate', 'SODueDate',
                    'SalesOrderCustomer__CustSID', 'SalesOrderCustomer__IsCompany', 'SalesOrderCustomer__CustCompany', 'SalesOrderCustomer__CustTitle', 'SalesOrderCustomer__CustFName', 'SalesOrderCustomer__CustLName', 'SalesOrderCustomer__CustPONumber',
                    'SalesOrderShipTo__ShipAddrName', 'SalesOrderShipTo__ShipCompany', 'SalesOrderShipTo__ShipFullName', 'SalesOrderShipTo__ShipAddress', 'SalesOrderShipTo__ShipAddress2', 'SalesOrderShipTo__ShipCity', 'SalesOrderShipTo__ShipState', 'SalesOrderShipTo__ShipZIP'],
    'sales_order_line': ['selected', 'SID', 'ItemPos', 'ItemSID', 'Qty', 'QtySent', 'Price', 'OrigPrice', 'DiscountLevel', 'DiscountPercent', 'Cost', 'TaxPercent', 'SalesOrderItemDesc__ItemNo', 'SalesOrderItemDesc__Desc1', 'SalesOrderItemDesc__Desc2', 'SalesOrderItemLongDesc__Desc2'],
    'purchase_order': ['selected', 'SID', 'PONum', 'Status', 'SuppVendSID', 'SuppVendCode', 'SuppVendCompany', 'Clerk', 'Comments', 'Total', 'TermsNetDays', 'PODate', 'POShipDate', 'POTotDueDate'],
    'purchase_order_line': ['selected', 'SID', 'ItemPos', 'ItemSID', 'Qty', 'QtyRcvd', 'Cost', 'PurchaseOrderItemDesc__ItemNo', 'PurchaseOrderItemDesc__Desc1', 'PurchaseOrderItemDesc__Desc2', 'PurchaseOrderItemLongDesc__Desc2'],
    'voucher': ['selected', 'SID', 'VoucherNum', 'VoucherType', 'VoucherStatus', 'DocRefSID', 'SuppVendSID', 'SuppVendCode', 'SuppVendCompany', 'Clerk', 'InvoiceNumber', 'Comments', 'Total', 'Freight', 'VoucherDate', 'VoucherTime', 'InvoiceDate', 'InvoiceDueDate'],
    'voucher_line': ['selected', 'SID', 'ItemPos', 'ItemSID', 'PORefSID', 'Qty', 'Cost', 'VoucherItemDesc__ItemNo', 'VoucherItemDesc__Desc1', 'VoucherItemDesc__Desc2', 'VoucherItemLongDesc__Desc2'],
}

# Wipe order respects foreign keys (children first). Printer, accounts and sessions are never touched.
# ReceiptOverride: Admin Override records of app test receipts go with them (PROTECT would block the wipe).
# OrderPaymentChange: the payment change log of app test orders goes with them, same reason.
WIPE_ORDER = [GiftRedemption, GiftCertificate, ReceiptOverride, OrderPaymentChange, Tender, Line, StockMovement, OrderPayment, Document, PurchaseLine, ReceivingMovement, PurchaseDocument, ItemVendor, InventoryAdjustment, Item, CustomerAddress, Customer, Vendor, Department]


def field(row, name):
    """A column that older callers (sqlite3.Row from a narrower SELECT) may not carry; None when absent."""
    return row[name] if name in row.keys() else None


def receipt_discount_fields(row):
    """Shared by rebuild and the additive header-reason backfill: the receipt discount's reason and percent
    (QB DiscPercent, kept to 2 places; the amount is imported separately and stays authoritative)."""
    return {'discount_reason': text(row['DiscountLevel'], 100), 'discount_percent': percent2(field(row, 'DiscPercent'))}


def order_discount_fields(row):
    """A QB sales order's whole-order discount: DiscAmount as recorded (authoritative; it is money the
    customer was not charged), DiscPercent to 2 places and DiscountLevel (QB stored none on a shop's orders).
    Shared by the importer and `manage.py backfill_order_discounts`. 170 orders in the 10/9 data carry one; every
    one recomputes exactly through the register's receipt-discount math."""
    return dict(discount=money(field(row, 'DiscAmount')) or Decimal('0'), **receipt_discount_fields(row))


def spread_order_discounts(orders):
    """Give each imported discounted order's lines their share of its discount (Line.receipt_discount), the way the
    register and QB spread a receipt's: QB keeps no per-line shares on order lines, and reports, the tax ledger and
    pickups read the net line amounts. Returns the number of lines given a share. Shared by the importer and the
    backfill command."""
    from .discounts import imported_order_shares
    changed = []
    for order in orders.prefetch_related('lines'):
        lines = list(order.lines.all())
        for line, share in imported_order_shares(lines, order.discount, order.discount_percent).items():
            if (line.receipt_discount or Decimal('0')) != share:
                line.receipt_discount = share
                changed.append(line)
    Line.objects.bulk_update(changed, ['receipt_discount'], batch_size=1000)
    return len(changed)


class RebuildError(Exception):
    pass


# ----- value helpers ---------------------------------------------------------------------------------------------

def dec(value, places=None):
    if value is None or value == '':
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise RebuildError(f'Bad decimal {value!r}') from exc
    if places is not None:
        quantized = result.quantize(Decimal(1).scaleb(-places))
        if quantized != result:
            raise RebuildError(f'Decimal {value!r} has more than {places} places')
        return quantized
    return result


def money(value):
    return dec(value, 2)


def qty(value):
    return dec(value, 3)


def text(value, limit=None):
    value = '' if value is None else str(value)
    return value[:limit] if limit else value


def day(value):
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def cents(value):
    """Whole cents, rounded half-up."""
    amount = dec(value)
    return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if amount is not None else Decimal('0.00')


def whole(value):
    """A QB price or cost in whole cents, rounded half-up (no fractional cents come in); None stays None."""
    amount = dec(value)
    return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if amount is not None else None


def price4(value):
    """A QB unit price kept to 4 decimals, as QB keeps % line discounts: 1241.9550 stays; the rare
    6-decimal price rounds half-up at the 4th place and keeps QB's line total in recorded_amount. None stays None."""
    amount = dec(value)
    return unit_price(amount) if amount is not None else None


def percent2(value):
    """A QB percent at 2 places (line DiscountPercent is always 2 or fewer; a typed-dollar receipt discount leaves a
    6-decimal DiscPercent, kept to 2 places for the register — the recorded amount stays authoritative)."""
    amount = dec(value)
    return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if amount is not None else Decimal('0.00')


def line_money_fields(r):
    """The money columns of a receipt/order line, shared by the importer and `manage.py backfill_unit_prices` so a
    backfilled line equals an imported one: the exact unit price (4 decimals), QB's line total when even that price
    cannot reproduce it, the % line discount, and the line's share of the receipt discount (ReceiptItem.RealPrice;
    sales order lines have none)."""
    price = price4(r['Price'])
    return dict(price=price, recorded_amount=qb_line_amount(qty(r['Qty']), dec(r['Price']), price),
                discount_percent=percent2(field(r, 'DiscountPercent')), receipt_discount=cents(field(r, 'RealPrice')))


def account_fields(r):
    """QB POS Customer Detail > QuickBooks Options: AR = Use with QuickBooks, UseAccCharge = Charge Account,
    CreditLimit = Account Limit, CreditUsed = Account Balance, Credit30/60/90Overdue = Past Due."""
    return dict(use_with_quickbooks=bool(r['AR']), charge_account=bool(r['UseAccCharge']), account_limit=cents(r['CreditLimit']),
                imported_account_balance=cents(r['CreditUsed']),
                imported_past_due=sum((cents(r[k]) for k in ('Credit30Overdue', 'Credit60Overdue', 'Credit90Overdue')), Decimal('0.00')),
                imported_last_sale=day(r['LastSale']))


def stamp(day_value, time_value):
    """Combine a QB date and time into an aware datetime in a shop's zone (noon when the time is missing)."""
    if not day_value:
        return None
    when = dtime(12)
    if time_value:
        when = dtime.fromisoformat(str(time_value)[:15])
    return datetime.combine(day(day_value), when, tzinfo=LOCAL_TZ)


def address(*parts):
    """Lines of an address: name/company lines, street lines, then 'City ST ZIP'."""
    *lines, city, state, postal = parts
    lines = [text(v).strip() for v in lines]
    last = ' '.join(text(v).strip() for v in (city, state, postal) if v and text(v).strip())
    return '\n'.join(v for v in [*lines, last] if v)


def party_name(is_company, company, title, first, last):
    """The name QuickBooks would print: company when flagged as one, otherwise title + first + last."""
    person = ' '.join(text(v).strip() for v in (title, first, last) if v and text(v).strip())
    if is_company and text(company).strip():
        return text(company).strip()
    return person or text(company).strip()


def valid_email(value):
    value = text(value).strip()
    return value if '@' in value and '.' in value.rsplit('@', 1)[-1] and ' ' not in value else ''


def terms(net_days):
    return f'{int(net_days)} days' if net_days else ''


class Counts(dict):
    def bump(self, key, by=1):
        self[key] = self.get(key, 0) + by


# ----- loading ---------------------------------------------------------------------------------------------------

class Loader:
    def __init__(self, con, counts, progress, history_from=None, balances_from=None, credits='history'):
        self.con = con
        self.counts = counts
        self.progress = progress or (lambda message: None)
        self.imported_at = timezone.now()
        self.created = {}
        self.history_from = history_from      # date: drop sales history before it; None = full history
        if balances_from is not None and history_from is not None and balances_from < history_from:
            raise RebuildError(f'balances_from {balances_from} is earlier than history_from {history_from}: a carried balance must come from loaded receipts')
        if credits not in CREDIT_RULES:
            raise RebuildError(f'credits must be one of {", ".join(CREDIT_RULES)}, not {credits!r}')
        self.balances_from = balances_from or history_from   # date: forgive what is due on charges before it; None = QB's figures
        self.credits = credits                # 'history' | 'all' | 'balances' — which credit balances carry
        self.dropped_receipts, self.dropped_orders, self.dropped_pos = set(), set(), set()
        self.account_charges = {}             # customer pk -> Account charges on completed receipts on/after balances_from
        self.account_credits = {}             # customer pk -> money put on the account (any Account tender < 0: payments on account, refunds) on/after balances_from
        self.active_customers = set()         # customer pks with a completed receipt in the history window
        self.balance_customers = set()        # customer pks with a completed receipt on/after balances_from

    def in_window(self, day_value, since=None):
        """True when a QB document date is on or after the history cutoff (always, without one; an undated row stays).
        `since` checks another date (the balance cutoff) the same way."""
        since = self.history_from if since is None else since
        if since is None:
            return True
        when = day(day_value)
        return when is None or when >= since

    def rows(self, table):
        columns = COLUMNS[table]
        cursor = self.con.execute(f'SELECT {", ".join(f"[{c}]" for c in columns)} FROM "{table}" WHERE selected = 1 ORDER BY rowid')
        while True:
            chunk = cursor.fetchmany(BATCH)
            if not chunk:
                return
            for row in chunk:
                yield dict(zip(columns, row))

    def save(self, model, objects):
        if objects:
            model.objects.bulk_create(objects, batch_size=BATCH)
        self.counts.bump(model._meta.label_lower.split('.')[-1] + '_rows', len(objects))
        return objects

    def child_saver(self, model, key):
        """Accumulates child rows and flushes them in batches so line/tender tables never sit in memory whole."""
        pending = []

        def add(obj):
            pending.append(obj)
            if len(pending) >= BATCH:
                flush()

        def flush():
            if pending:
                model.objects.bulk_create(pending, batch_size=BATCH)
                self.counts.bump(key, len(pending))
                pending.clear()
        return add, flush

    # -- masters --

    def departments(self):
        objects = []
        for r in self.rows('department'):
            objects.append(Department(source_id=r['SID'], code=text(r['DeptCode'], 40), name=text(r['DeptName'], 200) or text(r['DeptCode']) or r['SID'],
                                      tax_category='Non' if r['TaxCode'] == 1 else 'Taxable Sales', tax_rate=Decimal('0') if r['TaxCode'] == 1 else Decimal('7'),
                                      margin=dec(r['MarginPercent']) or Decimal('0'), active=True, source_data={k: v for k, v in r.items() if k != 'selected'}, imported_at=self.imported_at))
        self.save(Department, objects)
        self.depts = {d.source_id: d.pk for d in Department.objects.all()}
        self.counts['departments'] = len(objects)

    def vendors(self):
        objects = []
        for r in self.rows('vendor'):
            objects.append(Vendor(source_id=r['SID'], code=text(r['VendCode'], 40), company=text(r['Company'], 200) or text(r['VendCode']) or r['SID'],
                                  contact=' '.join(text(v).strip() for v in (r['FName'], r['LName']) if text(v).strip())[:200],
                                  street='\n'.join(text(v).strip() for v in (r['Address1'], r['Address2']) if text(v).strip()), city=text(r['City'], 100), state=text(r['State'], 100), postal_code=text(r['ZIP'], 40),
                                  phone=text(r['Phone1'], 100), fax=text(r['Phone2'], 100), email=valid_email(r['Email']), account_number=text(r['AccNum'], 100), terms=terms(r['TermsNetDays']),
                                  active=not r['IsInActive'], source_data={k: v for k, v in r.items() if k != 'selected'}, imported_at=self.imported_at))
        self.save(Vendor, objects)
        self.vendors_by_sid = {v.source_id: v.pk for v in Vendor.objects.all()}
        self.vendors_by_code = {v.code: v.pk for v in Vendor.objects.exclude(code='')}
        self.vendor_company = {v.pk: v.company for v in Vendor.objects.all()}
        self.counts['vendors'] = len(objects)

    def customers(self):
        objects, ship = [], {}
        for r in self.rows('customer'):
            names = dict(title=text(r['Title'], 50), first_name=text(r['FName'], 200), last_name=text(r['LName'], 200))
            full = ' '.join(v for v in names.values() if v)
            is_company = bool(r['IsCompany'])
            objects.append(Customer(source_id=r['SID'], customer_number=text(r['CustomerID'], 100), full_name=full[:500], company=text(r['Company'], 500), track_as_company=is_company, **names,
                                    street='\n'.join(text(v).strip() for v in (r['Address1'], r['Address2']) if text(v).strip()), city=text(r['City'], 200), state=text(r['State'], 100), postal_code=text(r['ZIP'], 100),
                                    phone=text(r['Phone1'], 100), mobile=text(r['Phone2'], 100), alternate_phone=text(r['Phone3'], 100), alternate_phone_2=text(r['Phone4'], 100), email=text(r['Email'], 500), notes=text(r['Comments']),
                                    tax_location=text(r['TaxArea'], 100), tax_exempt=text(r['TaxArea']).strip().casefold() == 'exempt', tax_rate=None, accept_checks=bool(r['AcceptChecks']), email_opt_in=bool(r['EmailNotify']),
                                    customer_type=text(r['Type'], 100), **account_fields(r), active=True, source_data={k: v for k, v in r.items() if k != 'selected'}, source_sha256=self.source_sha256))
            if text(r['ShipAddress']).strip():
                ship[r['SID']] = address(r['ShipAddrName'] or r['ShipCompany'] or r['ShipFullName'] or full, r['ShipAddress'], r['ShipAddress2'], r['ShipCity'], r['ShipState'], r['ShipZIP'])
        self.save(Customer, objects)
        self.customers_by_sid = {c.source_id: c.pk for c in Customer.objects.all()}
        addresses = [CustomerAddress(customer_id=self.customers_by_sid[sid], label='Shipping', address=value[:2000], is_default=True) for sid, value in ship.items()]
        self.save(CustomerAddress, addresses)
        self.counts['customers'] = len(objects)
        self.counts['customer_addresses'] = len(addresses)

    def items(self):
        objects = []
        for r in self.rows('item'):
            vendor_pk = self.vendors_by_sid.get(r['VendSID'])
            objects.append(Item(source_id=r['ItemSID'], sku=text(r['ItemNO'], 100), name=text(r['Desc1'], 200), description=text(r['InventoryLongDesc__Desc2'] or r['Desc2'] or r['Desc1'], 1000),
                                upc=text(r['UPC'], 100), alu=text(r['ALU'], 100), item_type='inventory' if r['QBItemType'] == 0 else 'noninventory', size=text(r['Size'], 100), wood=text(r['Attr'], 100),
                                unit_of_measure=text(r['UnitOfMeasure'], 50), manufacturer=text(r['Manufacturer'], 200),
                                tax_category='Non' if r['TaxCode'] == 1 else 'Taxable Sales', tax_rate=Decimal('0') if r['TaxCode'] == 1 else Decimal('7'),
                                price=whole(r['Price1']), price_2=whole(r['Price2']), price_3=whole(r['Price3']), price_4=whole(r['Price4']), price_5=whole(r['Price5']), price_6=whole(r['Price6']), msrp=whole(r['MSRP']),
                                order_cost=whole(r['LastCost']), average_cost=whole(r['Cost']), on_hand=qty(r['CompanyOHQty']), imported_on_order=qty(r['TotO_O']), reorder_point=qty(r['CmpMin']), imported_last_received=day(r['LastRecvd']),
                                print_tags=bool(r['PrintTag']), use_serial_numbers=bool(r['SerialFlag']), earn_commission=not r['NoCommission'], earn_rewards=bool(r['TrackRewards']),
                                shipping_weight=qty(r['Weight']), shipping_length=qty(r['Length']), shipping_width=qty(r['Width']), shipping_height=qty(r['Height']), comments=text(r['Comments']),
                                primary_vendor_id=vendor_pk, vendor=text(r['VendCode'], 200) if vendor_pk else '', department_id=self.depts.get(r['DeptSID']), active=True))
        self.save(Item, objects)
        self.item_pks = {i.source_id: i.pk for i in Item.objects.all()}
        self.counts['items'] = len(objects)
        alternates = []
        for r in self.rows('item_vendor'):
            vendor_pk = self.vendors_by_sid.get(r['VendSID'])
            item_pk = self.item_pks.get(r['ItemSID'])
            if not vendor_pk or not item_pk:
                self.counts.bump('item_vendors_skipped')
                continue
            alternates.append(ItemVendor(item_id=item_pk, vendor_id=vendor_pk, order_cost=whole(r['OrderCost'])))
        self.save(ItemVendor, alternates)
        self.counts['item_vendors'] = len(alternates)
        self.counts.setdefault('item_vendors_skipped', 0)

    # -- sales --

    def line_fields(self, r, prefix, sid_key='SID'):
        """Shared receipt/order line mapping from the ReceiptItemDesc-style snapshot columns."""
        item_no = r[f'{prefix}Desc__ItemNo']
        desc1 = text(r[f'{prefix}Desc__Desc1'], 200)
        description = text(r[f'{prefix}LongDesc__Desc2'] or r[f'{prefix}Desc__Desc2'] or desc1, 1000)
        item_pk = self.item_pks.get(r['ItemSID'])
        if not item_pk:
            self.counts.bump('receipt_lines_without_item' if prefix == 'ReceiptItem' else 'order_lines_without_item')
        return dict(source_id=f'{r[sid_key]}:{r["ItemPos"]}', position=r['ItemPos'], item_id=item_pk, sku='' if not item_no else text(item_no, 100), item_name=desc1, description=description or desc1 or 'Item',
                    quantity=qty(r['Qty']), original_price=whole(r['OrigPrice']), discount_reason=text(r['DiscountLevel'], 100),
                    tax_rate=dec(r['TaxPercent'], 3), unit_cost=whole(r['Cost']), **line_money_fields(r))

    def return_link(self, r):
        """QB's return -> original receipt link on a receipt line (DocRefSID) as a Document pk, or None. Shared by the
        importer and `manage.py backfill_return_links` so both store the same link. A return of a receipt dropped by
        the history cutoff keeps no link; counted separately."""
        sid = r.get('DocRefSID')
        if not sid:
            return None
        if sid in self.dropped_receipts:
            self.counts.bump('receipt_lines_return_link_before_cutoff')
            return None
        if not hasattr(self, 'receipts_by_sid'):
            self.receipts_by_sid = dict(Document.objects.filter(kind='sale', source_id__isnull=False).values_list('source_id', 'pk'))
        pk = self.receipts_by_sid.get(sid)
        self.counts.bump('receipt_lines_return_linked' if pk else 'receipt_lines_return_link_missing')
        return pk

    def receipts(self):
        lines_by_sid = {}
        objects = []
        balance_sids = set()   # kept receipts dated on/after the balance cutoff
        for r in self.rows('receipt'):
            if not self.in_window(r['ReceiptDate']):
                self.dropped_receipts.add(r['SID'])
                self.counts.bump('receipts_before_cutoff')
                continue
            if self.in_window(r['ReceiptDate'], self.balances_from):
                balance_sids.add(r['SID'])
            status = 'completed' if r['ReceiptStatus'] == 0 else 'cancelled'
            total = money(r['Total'])
            if total is None:
                self.counts.bump('receipt_totals_unresolved')
            when = stamp(r['ReceiptDate'], r['ReceiptTime'])
            customer_pk = self.customers_by_sid.get(r['BillToSID'])
            name = party_name(r['ReceiptCustomer__IsCompany'], r['ReceiptCustomer__CustCompany'], r['ReceiptCustomer__CustTitle'], r['ReceiptCustomer__CustFName'], r['ReceiptCustomer__CustLName'])
            shipping = address(r['ReceiptShipTo__ShipAddrName'] or r['ReceiptShipTo__ShipCompany'] or r['ReceiptShipTo__ShipFullName'], r['ReceiptShipTo__ShipAddress'], r['ReceiptShipTo__ShipAddress2'], r['ReceiptShipTo__ShipCity'], r['ReceiptShipTo__ShipState'], r['ReceiptShipTo__ShipZIP']) if text(r['ReceiptShipTo__ShipAddress']).strip() else ''
            objects.append(Document(source_id=r['SID'], source_number=r['ReceiptNum'], kind='sale', status=status, was_completed=True, receipt_type=r['ReceiptType'],
                                    customer_id=customer_pk, customer_name=name[:500], notes=text(r['Comments']), shipping_address=shipping[:2000], customer_po=text(r['ReceiptCustomer__CustPONumber'], 100),
                                    subtotal=money(r['SubTotal']) or Decimal('0'), tax=money(r['TaxAmount']), total=total, tax_exempt=(dec(r['TaxPercent']) or Decimal('0')) == 0,
                                    tax_location=text(r['TaxArea'], 100).strip(), discount=money(r['DiscAmount']) or Decimal('0'), **receipt_discount_fields(r),
                                    cashier=text(r['Cashier'], 100), clerk=text(r['Clerk'], 100), store=r['Store'], workstation=r['Workstation'], completed_at=when, created_at=when, updated_at=when, source_order=None))
            lines_by_sid[r['SID']] = None
        self.save(Document, objects)
        self.receipts_by_sid = dict(Document.objects.filter(kind='sale', source_id__isnull=False).values_list('source_id', 'pk'))
        self.counts['receipts'] = len(objects)
        receipt_customer = {}   # completed receipt pk -> customer pk (only needed for the cutoffs' balances)
        balance_receipts = {}   # the subset dated on/after the balance cutoff: these are the charges that can still be owed
        if self.balances_from is not None:
            receipt_customer = dict(Document.objects.filter(kind='sale', source_id__isnull=False, status='completed', customer__isnull=False).values_list('pk', 'customer_id'))
            self.active_customers = set(receipt_customer.values())
            sid_of = {pk: sid for sid, pk in self.receipts_by_sid.items()}
            balance_receipts = {pk: customer for pk, customer in receipt_customer.items() if sid_of[pk] in balance_sids}
            self.balance_customers = set(balance_receipts.values())
        if self.history_from is not None:
            self.counts.setdefault('receipts_before_cutoff', 0)
        add, flush = self.child_saver(Line, 'receipt_lines')
        line_sums = {}
        for r in self.rows('receipt_line'):
            if r['SID'] in self.dropped_receipts:
                self.counts.bump('receipt_lines_before_cutoff')
                continue
            doc_pk = self.receipts_by_sid.get(r['SID'])
            if doc_pk is None:
                self.counts.bump('receipt_lines_orphaned')
                continue
            fields = self.line_fields(r, 'ReceiptItem')
            line_sums[doc_pk] = line_sums.get(doc_pk, Decimal('0')) + line_amount(fields['quantity'], fields['price'], fields['recorded_amount'])
            add(Line(document_id=doc_pk, returned_from_receipt_id=self.return_link(r), **fields))
        flush()
        self.counts.setdefault('receipt_lines', 0)
        self.counts.setdefault('receipt_lines_without_item', 0)
        # Tenders; per receipt: tendered = sum(amount where not use_in_total), payment_method from the counted rows.
        add, flush = self.child_saver(Tender, 'tenders')
        tendered, methods = {}, {}
        account_by_doc = {}   # completed receipt pk -> its counted Account tenders (a charge when > 0; balances)
        for r in self.rows('receipt_tender'):
            if r['SID'] in self.dropped_receipts:
                self.counts.bump('tenders_before_cutoff')
                continue
            doc_pk = self.receipts_by_sid.get(r['SID'])
            if doc_pk is None:
                self.counts.bump('tenders_orphaned')
                continue
            amount = money(r['TenderAmount'])
            if amount is None:
                self.counts.bump('tender_amounts_unresolved')
            method = TENDER_METHODS.get(r['TenderType'], 'other')
            if method == 'other' and r['TenderType'] not in TENDER_METHODS:
                self.counts.bump('tender_types_unmapped')
            use_in_total = bool(r['TenderUseInTotal'])
            add(Tender(document_id=doc_pk, source_id=f'{r["SID"]}:{r["TenderPos"]}', position=r['TenderPos'], method=method, amount=amount,
                       tender_name=text(r['TenderName'], 100) or TENDER_NAMES.get(r['TenderType'], ''), reference=text(r['TenderNumber'], 100), use_in_total=use_in_total))
            if method == 'account' and amount is not None and amount < 0 and doc_pk in balance_receipts:
                # Money put on the customer's account on/after the balance date (a payment on account's memo row, a
                # refund to the account): what a store credit can be made of.
                customer_pk = balance_receipts[doc_pk]
                self.account_credits[customer_pk] = self.account_credits.get(customer_pk, Decimal('0')) - amount
            if not use_in_total:
                if amount is None:
                    tendered[doc_pk] = None
                elif doc_pk not in tendered or tendered[doc_pk] is not None:
                    tendered[doc_pk] = tendered.get(doc_pk, Decimal('0')) + amount
                methods.setdefault(doc_pk, set()).add(method)
                if method == 'account' and amount is not None and doc_pk in balance_receipts:
                    account_by_doc[doc_pk] = account_by_doc.get(doc_pk, Decimal('0')) + amount
        flush()
        for doc_pk, charged in account_by_doc.items():
            if charged > 0:   # the same rule as reports.tax_ledger.tenders: counted Account rows summed per receipt
                customer_pk = balance_receipts[doc_pk]
                self.account_charges[customer_pk] = self.account_charges.get(customer_pk, Decimal('0')) + charged
        self.counts.setdefault('tenders', 0)
        self.counts.setdefault('tender_amounts_unresolved', 0)
        self.counts.setdefault('receipt_totals_unresolved', 0)
        # Header summaries from the tender rows, and the amount sanity check (counted, not fatal: discounts).
        mismatches = 0
        by_method = {}
        for doc_pk, found in methods.items():
            by_method.setdefault((next(iter(found)) if len(found) == 1 else 'split'), []).append(doc_pk)
        for method, pks in by_method.items():
            for start in range(0, len(pks), BATCH):
                Document.objects.filter(pk__in=pks[start:start + BATCH]).update(payment_method=method)
        by_amount = {}
        for doc_pk, amount in tendered.items():
            if amount is not None:
                by_amount.setdefault(amount, []).append(doc_pk)
        for amount, pks in by_amount.items():
            for start in range(0, len(pks), BATCH):
                Document.objects.filter(pk__in=pks[start:start + BATCH]).update(tendered=amount)
        for doc_pk, subtotal in Document.objects.filter(kind='sale', source_id__isnull=False, receipt_type=0).values_list('pk', 'subtotal').iterator(chunk_size=BATCH):
            if line_sums.get(doc_pk, Decimal('0')).quantize(Decimal('0.01')) != subtotal:
                mismatches += 1
        self.counts['receipt_subtotal_mismatches'] = mismatches

    def carried_balances(self):
        """Balance cutoff: a stored QB balance carries only as far as it comes from Account
        charges on or after the balance cutoff (`balances_from`, the history cutoff unless given separately) — payments
        apply FIFO, so what is still owed is the newest charges — and what is due on older charges is forgiven. Past due
        is capped at the carried balance. A credit balance (money owed to the customer) follows the `credits` rule:
        'history' keeps it for customers with a completed receipt in the history window (all of them without a history
        cutoff), 'all' keeps every credit, 'balances' ages the credit itself: what carries is at most the
        credits put on the account on/after the balance cutoff, the oldest credit spent first, mirroring the debt rule.
        Nothing to do without a cutoff (QB's figures stay)."""
        if self.balances_from is None:
            return
        zero = Decimal('0.00')
        totals = Counts()
        largest = {'balances_largest_carried': zero, 'balances_largest_dropped': zero}
        changed = []
        if self.credits == 'balances':
            credit_keepers = 'aged'
        elif self.credits == 'history' and self.history_from is not None:
            credit_keepers = self.active_customers
        else:
            credit_keepers = None   # keep them all
        for c in Customer.objects.filter(source_id__isnull=False).exclude(imported_account_balance=0).only('pk', 'imported_account_balance', 'imported_past_due'):
            stored = c.imported_account_balance
            if stored > 0:
                carried = min(stored, self.account_charges.get(c.pk, zero))
                kind = 'balances'
            else:
                if credit_keepers == 'aged':
                    carried = -min(-stored, self.account_credits.get(c.pk, zero))
                else:
                    carried = stored if credit_keepers is None or c.pk in credit_keepers else zero
                kind = 'credits'
            if carried == stored:
                totals.bump(f'{kind}_carried')
                totals.bump(f'{kind}_carried_total', carried)
                if kind == 'balances':
                    totals.bump('balances_carried_in_full')
            elif carried == zero:
                totals.bump(f'{kind}_dropped')
                totals.bump(f'{kind}_dropped_total', stored)
            else:
                totals.bump(f'{kind}_carried')
                totals.bump(f'{kind}_carried_partly')
                totals.bump(f'{kind}_carried_total', carried)
                totals.bump(f'{kind}_dropped_total', stored - carried)
            if kind == 'balances':
                largest['balances_largest_carried'] = max(largest['balances_largest_carried'], carried)
                largest['balances_largest_dropped'] = max(largest['balances_largest_dropped'], stored - carried)
            if carried != stored:
                c.imported_account_balance = carried
                c.imported_past_due = min(c.imported_past_due or zero, max(carried, zero))
                changed.append(c)
        Customer.objects.bulk_update(changed, ['imported_account_balance', 'imported_past_due'], batch_size=BATCH)
        for key in ('balances_carried', 'balances_carried_in_full', 'balances_carried_partly', 'balances_dropped', 'credits_carried', 'credits_carried_partly', 'credits_dropped'):
            totals.setdefault(key, 0)
        for key in ('balances_carried_total', 'balances_dropped_total', 'credits_carried_total', 'credits_dropped_total'):
            totals.setdefault(key, zero)
        self.counts.update(totals)
        self.counts.update(largest)
        self.counts['customers_balance_changed'] = len(changed)

    def held_receipts(self, con):
        """QB POS held receipts (Receipt DataState = 1) and their lines, as app held receipts.

        `con` is the main dataset when it keeps DataState-1 rows (the 10/2 export does, unselected) or the
        held.sqlite companion of a 9/27-style current.sqlite (build_held.py). Held receipts get no receipt number
        (QB shows none and reuses the provisional one), never move stock, and become app receipts when someone
        works on them. Prices come in at whole cents, rounded half-up (rule)."""
        if not hasattr(self, 'customers_by_sid'):
            self.customers_by_sid = dict(Customer.objects.exclude(source_id=None).values_list('source_id', 'pk'))
        if not hasattr(self, 'item_pks'):
            self.item_pks = dict(Item.objects.exclude(source_id=None).values_list('source_id', 'pk'))
        receipt_cols, line_cols = COLUMNS['receipt'], COLUMNS['receipt_line']
        objects = []
        for row in con.execute(f'SELECT {", ".join(f"[{c}]" for c in receipt_cols)} FROM receipt WHERE data_state = 1 ORDER BY rowid'):
            r = dict(zip(receipt_cols, row))
            when = stamp(r['ReceiptDate'], r['ReceiptTime'])
            name = party_name(r['ReceiptCustomer__IsCompany'], r['ReceiptCustomer__CustCompany'], r['ReceiptCustomer__CustTitle'], r['ReceiptCustomer__CustFName'], r['ReceiptCustomer__CustLName'])
            customer_pk = self.customers_by_sid.get(r['BillToSID'])
            if r['BillToSID'] and not customer_pk:
                self.counts.bump('held_receipts_customer_missing')
            exempt = (dec(r['TaxPercent']) or Decimal('0')) == 0
            objects.append(Document(source_id=r['SID'], source_number=None, kind='sale', status='held', was_completed=False, receipt_type=r['ReceiptType'],
                                    customer_id=customer_pk, customer_name=name[:500], notes=text(r['Comments']), customer_po=text(r['ReceiptCustomer__CustPONumber'], 100),
                                    subtotal=cents(r['SubTotal']), tax=cents(r['TaxAmount']), total=cents(r['Total']), tax_exempt=exempt,
                                    tax_location=text(r['TaxArea'], 100).strip(), discount=cents(r['DiscAmount']), cashier=text(r['Cashier'], 100), clerk=text(r['Clerk'], 100), **receipt_discount_fields(r),
                                    store=r['Store'], workstation=r['Workstation'], completed_at=None, created_at=when, updated_at=when))
        self.save(Document, objects)
        held = dict(Document.objects.filter(kind='sale', status='held', source_id__in=[d.source_id for d in objects]).values_list('source_id', 'pk'))
        self.counts['held_receipts'] = len(objects)
        lines = []
        placeholders = ','.join('?' * len(held))
        if held:
            query = f'SELECT {", ".join(f"[{c}]" for c in line_cols)} FROM receipt_line WHERE SID IN ({placeholders}) ORDER BY SID, ItemPos'
            for row in con.execute(query, list(held)):
                r = dict(zip(line_cols, row))
                fields = self.line_fields(r, 'ReceiptItem')  # prices kept to 4 decimals as QB has them
                if r['Price'] is not None and dec(r['Price']) != fields['price']:
                    self.counts.bump('held_receipt_prices_truncated')  # only prices beyond 4 decimals
                lines.append(Line(document_id=held[r['SID']], returned_from_receipt_id=self.return_link(r), **fields))
        self.save(Line, lines)
        self.counts['held_receipt_lines'] = len(lines)

    def orders(self):
        objects = []
        for r in self.rows('sales_order'):
            if not self.in_window(r['SODate']):
                self.dropped_orders.add(r['SID'])
                self.counts.bump('open_orders_before_cutoff' if r['Status'] == 0 else 'closed_orders_before_cutoff')
                continue
            number = text(r['SONum']).strip()
            notes = text(r['Comments'])
            if not number.isdigit():
                self.counts.bump('orders_without_number')
                notes = f'QuickBooks order number: {number or "(blank)"}\n{notes}'.strip()
            when = stamp(r['SODate'], None)
            name = party_name(r['SalesOrderCustomer__IsCompany'], r['SalesOrderCustomer__CustCompany'], r['SalesOrderCustomer__CustTitle'], r['SalesOrderCustomer__CustFName'], r['SalesOrderCustomer__CustLName'])
            shipping = address(r['SalesOrderShipTo__ShipAddrName'] or r['SalesOrderShipTo__ShipCompany'] or r['SalesOrderShipTo__ShipFullName'], r['SalesOrderShipTo__ShipAddress'], r['SalesOrderShipTo__ShipAddress2'], r['SalesOrderShipTo__ShipCity'], r['SalesOrderShipTo__ShipState'], r['SalesOrderShipTo__ShipZIP']) if text(r['SalesOrderShipTo__ShipAddress']).strip() else ''
            status = 'open' if r['Status'] == 0 else 'completed'
            objects.append(Document(source_id=r['SID'], source_number=int(number) if number.isdigit() else None, kind='order', status=status, was_completed=status == 'completed',
                                    customer_id=self.customers_by_sid.get(r['BillToSID']), customer_name=name[:500], notes=notes, shipping_address=shipping[:2000], customer_po=text(r['SalesOrderCustomer__CustPONumber'], 100),
                                    subtotal=money(r['SubTotal']) or Decimal('0'), tax=money(r['TaxAmount']), total=money(r['Total']), tax_exempt=(dec(r['TaxPercent']) or Decimal('0')) == 0,
                                    deposit_taken=money(r['DepositTaken']), deposit_used=money(r['DepositUsed']), due_date=day(r['SODueDate']), cashier=text(r['Cashier'], 100), clerk=text(r['Clerk'], 100), store=r['Store'],
                                    **order_discount_fields(r), completed_at=when if status == 'completed' else None, created_at=when, updated_at=when))
        self.save(Document, objects)
        self.counts['orders'] = len(objects)
        self.counts['orders_with_discount'] = sum(1 for d in objects if d.discount)
        self.counts.setdefault('orders_without_number', 0)
        orders_by_sid = dict(Document.objects.filter(kind='order', source_id__isnull=False).values_list('source_id', 'pk'))
        self.link_deposits(orders_by_sid)
        add, flush = self.child_saver(Line, 'order_lines')
        for r in self.rows('sales_order_line'):
            if r['SID'] in self.dropped_orders:
                self.counts.bump('order_lines_before_cutoff')
                continue
            doc_pk = orders_by_sid.get(r['SID'])
            if doc_pk is None:
                self.counts.bump('order_lines_orphaned')
                continue
            add(Line(document_id=doc_pk, **self.line_fields(r, 'SalesOrderItem')))
        flush()
        self.counts.setdefault('order_lines', 0)
        self.counts.setdefault('order_lines_without_item', 0)
        self.counts['order_lines_with_discount_share'] = spread_order_discounts(Document.objects.filter(kind='order', source_id__isnull=False).exclude(discount=0))
        if self.history_from is not None:
            for key in ('open_orders_before_cutoff', 'closed_orders_before_cutoff', 'order_lines_before_cutoff'):
                self.counts.setdefault(key, 0)

    def link_deposits(self, orders_by_sid):
        """Point each deposit / deposit-refund receipt at the customer order it paid toward (SORefSID). With a history
        cutoff, a deposit in the window on a dropped (pre-cutoff) order stays a deposit with no order link — the money
        was received in the window — and is counted with its total; a dropped deposit on a kept order is counted too
        (the order's own DepositTaken from QB still tells what it holds)."""
        links = {}
        zero = Decimal('0.00')
        for r in self.rows('receipt'):
            if r['ReceiptType'] in (2, 3) and r['SORefSID']:
                if r['SID'] in self.dropped_receipts:
                    if r['SORefSID'] not in self.dropped_orders and r['SORefSID'] in orders_by_sid:
                        self.counts.bump('deposits_before_cutoff_on_kept_order')
                    continue
                if r['SORefSID'] in self.dropped_orders:
                    self.counts.bump('deposits_on_orders_before_cutoff')
                    self.counts.bump('deposits_on_orders_before_cutoff_total', money(r['Total']) or zero)
                    continue
                order_pk = orders_by_sid.get(r['SORefSID'])
                if order_pk is None:
                    self.counts.bump('deposits_without_order')
                else:
                    links.setdefault(order_pk, []).append(r['SID'])
        for order_pk, sids in links.items():
            Document.objects.filter(kind='sale', source_id__in=sids).update(deposit_order_id=order_pk)
        self.counts['deposits_linked'] = sum(len(v) for v in links.values())
        self.counts.setdefault('deposits_without_order', 0)
        if self.history_from is not None:
            self.counts.setdefault('deposits_on_orders_before_cutoff', 0)
            self.counts.setdefault('deposits_on_orders_before_cutoff_total', zero)
            self.counts.setdefault('deposits_before_cutoff_on_kept_order', 0)

    # -- purchasing --

    def vendor_for(self, sid, code, kind):
        vendor_pk = self.vendors_by_sid.get(sid) or self.vendors_by_code.get(text(code).strip())
        if not vendor_pk:
            self.counts.bump(f'{kind}_without_vendor')
        return vendor_pk

    def purchase_orders(self):
        objects = []
        received_in_window = set()   # PO SIDs a voucher in the window received against (such a closed PO was closed in the window)
        if self.history_from is not None:
            window_vouchers = {r['SID'] for r in self.rows('voucher') if self.in_window(r['VoucherDate'])}
            received_in_window = {r['PORefSID'] for r in self.rows('voucher_line') if r['PORefSID'] and r['SID'] in window_vouchers}
        for r in self.rows('purchase_order'):
            if r['Status'] != 0 and not self.in_window(r['PODate']):
                if r['SID'] in received_in_window:
                    self.counts.bump('purchase_orders_kept_for_window_voucher')
                else:
                    self.dropped_pos.add(r['SID'])
                    self.counts.bump('closed_purchase_orders_before_cutoff')
                    continue
            elif r['Status'] == 0 and not self.in_window(r['PODate']):
                self.counts.bump('open_purchase_orders_kept_before_cutoff')
            vendor_pk = self.vendor_for(r['SuppVendSID'], r['SuppVendCode'], 'purchase_orders')
            when = stamp(r['PODate'], None)
            objects.append(PurchaseDocument(source_id=r['SID'], source_number=text(r['PONum'], 40).strip(), kind='po', status='open' if r['Status'] == 0 else 'closed',
                                            vendor_id=vendor_pk, vendor_name=text(r['SuppVendCompany'], 200) or (self.vendor_company.get(vendor_pk, '') if vendor_pk else '') or text(r['SuppVendCode'], 200),
                                            notes=text(r['Comments']), terms=terms(r['TermsNetDays']), ship_date=day(r['POShipDate']), payment_due=day(r['POTotDueDate']), total=money(r['Total']) or Decimal('0'),
                                            clerk=text(r['Clerk'], 100), created_at=when, updated_at=when))
        self.save(PurchaseDocument, objects)
        self.counts['purchase_orders'] = len(objects)
        self.counts.setdefault('purchase_orders_without_vendor', 0)
        self.pos_by_sid = dict(PurchaseDocument.objects.filter(kind='po', source_id__isnull=False).values_list('source_id', 'pk'))
        add, flush = self.child_saver(PurchaseLine, 'purchase_order_lines')
        self.po_lines = {}  # (po pk, item sid) -> line source_id or None when ambiguous
        for r in self.rows('purchase_order_line'):
            if r['SID'] in self.dropped_pos:
                self.counts.bump('purchase_order_lines_before_cutoff')
                continue
            doc_pk = self.pos_by_sid.get(r['SID'])
            if doc_pk is None:
                self.counts.bump('purchase_order_lines_orphaned')
                continue
            quantity, received = qty(r['Qty']), qty(r['QtyRcvd']) or Decimal('0')
            if received > quantity:
                self.counts.bump('purchase_received_overflow')
                received = quantity
            if received < 0:
                self.counts.bump('purchase_received_negative')
                received = Decimal('0')
            item_pk = self.item_pks.get(r['ItemSID'])
            if not item_pk:
                self.counts.bump('purchase_order_lines_without_item')
            desc1 = text(r['PurchaseOrderItemDesc__Desc1'], 200)
            source_id = f'{r["SID"]}:{r["ItemPos"]}'
            key = (doc_pk, r['ItemSID'])
            self.po_lines[key] = None if key in self.po_lines else source_id
            add(PurchaseLine(document_id=doc_pk, source_id=source_id, position=r['ItemPos'], item_id=item_pk, sku=text(r['PurchaseOrderItemDesc__ItemNo'], 100) if r['PurchaseOrderItemDesc__ItemNo'] else '', item_name=desc1,
                             description=text(r['PurchaseOrderItemLongDesc__Desc2'] or r['PurchaseOrderItemDesc__Desc2'] or desc1, 1000) or desc1 or 'Item',
                             quantity=quantity, cost=whole(r['Cost']), recorded_amount=qb_line_amount(quantity, dec(r['Cost']), whole(r['Cost'])), received=received, imported_received=received))
        flush()
        self.counts.setdefault('purchase_order_lines', 0)
        self.counts.setdefault('purchase_received_overflow', 0)
        self.po_line_pks = dict(PurchaseLine.objects.filter(document__kind='po', source_id__isnull=False).values_list('source_id', 'pk'))

    def vouchers(self):
        objects = []
        first_po = {}
        dropped_vouchers = set()
        for r in self.rows('voucher_line'):
            if r['PORefSID'] and r['PORefSID'] in self.pos_by_sid:
                first_po.setdefault(r['SID'], self.pos_by_sid[r['PORefSID']])
        for r in self.rows('voucher'):
            if not self.in_window(r['VoucherDate']):
                dropped_vouchers.add(r['SID'])
                self.counts.bump('vouchers_before_cutoff')
                if first_po.get(r['SID']) is not None:
                    self.counts.bump('vouchers_before_cutoff_on_kept_po')
                continue
            vendor_pk = self.vendor_for(r['SuppVendSID'], r['SuppVendCode'], 'vouchers')
            when = stamp(r['VoucherDate'], r['VoucherTime'])
            po_pk = first_po.get(r['SID'])
            if po_pk is None:
                self.counts.bump('vouchers_without_po')
            objects.append(PurchaseDocument(source_id=r['SID'], source_number=text(r['VoucherNum'], 40), kind='voucher', status='posted' if r['VoucherStatus'] == 0 else 'cancelled',
                                            vendor_id=vendor_pk, vendor_name=text(r['SuppVendCompany'], 200) or (self.vendor_company.get(vendor_pk, '') if vendor_pk else '') or text(r['SuppVendCode'], 200),
                                            source_po_id=po_pk, notes=text(r['Comments']), invoice_number=text(r['InvoiceNumber'], 100), invoice_date=day(r['InvoiceDate']), payment_due=day(r['InvoiceDueDate']),
                                            freight=money(r['Freight']), total=money(r['Total']) or Decimal('0'), clerk=text(r['Clerk'], 100), posted_at=when, created_at=when, updated_at=when))
        self.save(PurchaseDocument, objects)
        self.counts['vouchers'] = len(objects)
        self.counts.setdefault('vouchers_without_po', 0)
        self.counts.setdefault('vouchers_without_vendor', 0)
        if self.history_from is not None:
            for key in ('vouchers_before_cutoff', 'vouchers_before_cutoff_on_kept_po', 'closed_purchase_orders_before_cutoff', 'purchase_orders_kept_for_window_voucher',
                        'open_purchase_orders_kept_before_cutoff', 'purchase_order_lines_before_cutoff', 'voucher_lines_before_cutoff'):
                self.counts.setdefault(key, 0)
        vouchers_by_sid = dict(PurchaseDocument.objects.filter(kind='voucher', source_id__isnull=False).values_list('source_id', 'pk'))
        add, flush = self.child_saver(PurchaseLine, 'voucher_lines')
        for r in self.rows('voucher_line'):
            if r['SID'] in dropped_vouchers:
                self.counts.bump('voucher_lines_before_cutoff')
                continue
            doc_pk = vouchers_by_sid.get(r['SID'])
            if doc_pk is None:
                self.counts.bump('voucher_lines_orphaned')
                continue
            item_pk = self.item_pks.get(r['ItemSID'])
            if not item_pk:
                self.counts.bump('voucher_lines_without_item')
            po_pk = self.pos_by_sid.get(r['PORefSID']) if r['PORefSID'] else None
            source_line_pk = self.po_line_pks.get(self.po_lines.get((po_pk, r['ItemSID']))) if po_pk else None
            if source_line_pk is None:
                self.counts.bump('voucher_lines_without_po_line')
            desc1 = text(r['VoucherItemDesc__Desc1'], 200)
            quantity = qty(r['Qty'])
            add(PurchaseLine(document_id=doc_pk, source_id=f'{r["SID"]}:{r["ItemPos"]}', position=r['ItemPos'], item_id=item_pk, sku=text(r['VoucherItemDesc__ItemNo'], 100) if r['VoucherItemDesc__ItemNo'] else '', item_name=desc1,
                             description=text(r['VoucherItemLongDesc__Desc2'] or r['VoucherItemDesc__Desc2'] or desc1, 1000) or desc1 or 'Item',
                             quantity=quantity, cost=whole(r['Cost']), recorded_amount=qb_line_amount(quantity, dec(r['Cost']), whole(r['Cost'])), received=quantity, imported_received=quantity, source_line_id=source_line_pk))
        flush()
        self.counts.setdefault('voucher_lines', 0)
        self.counts.setdefault('voucher_lines_without_po_line', 0)


class source_timestamps:
    """Let bulk_create keep the QB dates: auto_now/auto_now_add would otherwise overwrite created_at/updated_at."""

    def __enter__(self):
        self.fields = [model._meta.get_field(name) for model in (Document, PurchaseDocument) for name in ('created_at', 'updated_at')]
        self.saved = [(f.auto_now, f.auto_now_add) for f in self.fields]
        for f in self.fields:
            f.auto_now = f.auto_now_add = False

    def __exit__(self, *exc):
        for f, (now, add) in zip(self.fields, self.saved):
            f.auto_now, f.auto_now_add = now, add


# ----- entry point -----------------------------------------------------------------------------------------------

def open_source(path):
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        con.execute('PRAGMA query_only = ON')
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as exc:
        raise RebuildError(f'Cannot open {path}: {exc}') from exc
    missing = (set(COLUMNS) | {'meta', 'controls'}) - tables
    if missing:
        con.close()
        raise RebuildError(f'{path} is not a current.sqlite dataset; missing tables: {", ".join(sorted(missing))}')
    return con


def validate(con):
    meta = dict(con.execute('SELECT key, value FROM meta'))
    sha = meta.get('source_sha256')
    if not sha or len(sha) != 64:
        raise RebuildError('meta.source_sha256 is missing: the dataset is not traceable to a sealed backup.')
    bad = [(subject, metric, value) for subject, metric, value in con.execute("SELECT subject, metric, value FROM controls WHERE metric LIKE '%mismatch%'") if str(value).strip() not in ('', '0')]
    if bad:
        raise RebuildError('controls report value mismatches; refusing to load: ' + ', '.join(f'{s}.{m}={v}' for s, m, v in bad))
    return meta


def has_held(con):
    columns = {row[1] for row in con.execute('PRAGMA table_info(receipt)')}
    return 'data_state' in columns and con.execute('SELECT 1 FROM receipt WHERE data_state = 1 LIMIT 1').fetchone() is not None


def held_source(con, path, meta):
    """Where the held receipts are: the dataset itself if it kept DataState-1 receipts, else held.sqlite beside it
    (same source backup). Returns (connection, path) or (None, None)."""
    if has_held(con):
        return con, path
    source = Path(path)
    # held.sqlite beside current.sqlite, or held-<date>.sqlite beside current-<date>.sqlite (server naming)
    candidates = [source.with_name('held.sqlite'), source.with_name(source.name.replace('current', 'held', 1))]
    companion = next((c for c in candidates if c != source and c.is_file()), None)
    if companion is None:
        return None, None
    held = sqlite3.connect(f'file:{companion}?mode=ro', uri=True)
    held.execute('PRAGMA query_only = ON')
    held_meta = dict(held.execute('SELECT key, value FROM meta'))
    if held_meta.get('source_sha256') != meta.get('source_sha256'):
        held.close()
        raise RebuildError(f'{companion} was built from a different backup than {path}.')
    return held, companion


def load_held_receipts(path, commit=False, progress=None):
    """Replace the QB held receipts (only those still untouched) from a dataset or held.sqlite, keeping all other data."""
    say = progress or (lambda message: None)
    con = open_held(path)
    counts = Counts()
    try:
        meta = dict(con.execute('SELECT key, value FROM meta'))
        with transaction.atomic(), source_timestamps():
            stale = Document.objects.filter(kind='sale', status='held', source_id__isnull=False)
            counts['held_receipts_replaced'] = stale.count()
            Line.objects.filter(document__in=stale).delete()
            stale.delete()
            loader = Loader(con, counts, say)
            loader.held_receipts(con)
            if not commit:
                transaction.set_rollback(True)
    finally:
        con.close()
    return dict(source_sha256=meta.get('source_sha256', ''), counts=counts, committed=commit)


def open_held(path):
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        con.execute('PRAGMA query_only = ON')
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as exc:
        raise RebuildError(f'Cannot open {path}: {exc}') from exc
    if not {'receipt', 'receipt_line', 'meta'} <= tables:
        con.close()
        raise RebuildError(f'{path} has no receipt/receipt_line/meta tables.')
    if not has_held(con):
        con.close()
        raise RebuildError(f'{path} has no held receipts (DataState 1).')
    return con


def native_completed():
    return Document.objects.filter(source_id__isnull=True, status='completed').count() + PurchaseDocument.objects.filter(source_id__isnull=True, status__in=['posted', 'closed']).count()


def reset_document_numbers(continue_qb_numbers=False):
    from django.db.models import Max
    from .models import DocumentNumberCounter
    starts = {}
    for kind, testing_start in [('sale', 200000), ('order', 10000)]:
        last = Document.objects.filter(kind=kind, source_id__isnull=False).aggregate(last=Max('source_number'))['last'] or 0
        start = last + 1 if continue_qb_numbers else testing_start
        DocumentNumberCounter.objects.update_or_create(kind=kind, defaults={'next_number': start})
        starts[kind] = start
    return starts


def relink_tag_lines():
    from tags.models import FurnitureSetLine
    items = dict(Item.objects.values_list('sku', 'pk'))
    linked = 0
    for line in FurnitureSetLine.objects.filter(item__isnull=True).exclude(sku='').iterator():
        if line.sku in items:
            FurnitureSetLine.objects.filter(pk=line.pk).update(item_id=items[line.sku])
            linked += 1
    unlinked = FurnitureSetLine.objects.filter(item__isnull=True)
    return dict(relinked_tag_lines=linked, unmatched_tag_lines=unlinked.exclude(sku='').count(), tag_lines_without_item_number=unlinked.filter(sku='').count())


def history_cutoff(years=5, cutover=None):
    """The history cutoff as a rule: `years` back from the cutover day (today unless given), rounded down to the
    start of the sales-tax quarter in the shop's tax year (Tax Settings). With New York's March tax year (quarters start
    Mar/Jun/Sep/Dec 1): 10/15/2026 - 5 years = 10/15/2021 -> 9/1/2021. An explicit `--history-from` date always wins."""
    from reports.tax_periods import quarter_of, quarter_range
    on = cutover or timezone.localdate()
    back = date(on.year - years, on.month, min(on.day, 28))   # Feb 29 safe; the day never matters after rounding
    return quarter_range(*quarter_of(back))[0]


def rebuild_from_backup(path, commit=False, discard_test_data=False, progress=None, continue_qb_numbers=False, history_from=None, balances_from=None, credits='history'):
    """Validate, and with commit=True wipe business data and load the dataset. Returns counts and metadata.
    `history_from` (a date) drops sales history before it; `balances_from` (a date, on/after history_from) forgives
    what is due on Account charges before it; `credits` says which credit balances carry — see the module docstring."""
    started = time.monotonic()
    say = progress or (lambda message: None)
    balance_date = balances_from or history_from
    if balances_from is not None and history_from is not None and balances_from < history_from:
        raise RebuildError(f'balances_from {balances_from} is earlier than history_from {history_from}: a carried balance must come from loaded receipts')
    if credits not in CREDIT_RULES:
        raise RebuildError(f'credits must be one of {", ".join(CREDIT_RULES)}, not {credits!r}')
    con = open_source(path)
    try:
        meta = validate(con)
        counts = Counts()
        table_counts = {table: con.execute(f'SELECT count(*) FROM "{table}" WHERE selected = 1').fetchone()[0] for table in COLUMNS}
        result = dict(source_sha256=meta['source_sha256'], content_sha256=meta.get('content_sha256', ''), source_rows=table_counts, counts=counts, committed=False,
                      history_from=history_from.isoformat() if history_from else None,
                      balances_from=balance_date.isoformat() if balance_date else None, credits=credits)
        native = native_completed()
        if native and not discard_test_data:
            raise RebuildError(f'{native} completed document(s) were created in this app, not imported. Re-run with --discard-test-data to drop them.')
        result['discarded_native_documents'] = Document.objects.filter(source_id__isnull=True).count() + PurchaseDocument.objects.filter(source_id__isnull=True).count()
        if history_from:
            say(f'history cutoff: sales documents from {history_from:%-m/%-d/%Y} on; all customers and items; held receipts and open POs regardless of date')
        if balance_date:
            say(f'balance cutoff: what is still due on Account charges before {balance_date:%-m/%-d/%Y} is forgiven (payments FIFO); credits: {credits}')
        with transaction.atomic(), source_timestamps():
            # Snapshot identity before SET_NULL removes the live link.
            from tags.models import FurnitureSetLine
            for line in FurnitureSetLine.objects.filter(item__isnull=False).select_related('item').iterator():
                FurnitureSetLine.objects.filter(pk=line.pk).update(sku=line.item.sku)
            say('wiping business tables')
            for model in WIPE_ORDER:
                if model is PurchaseDocument:
                    model.objects.filter(kind='voucher').delete()  # vouchers protect their source PO
                model.objects.all().delete()
            ItemNumberCounter.objects.all().delete()
            loader = Loader(con, counts, say, history_from=history_from, balances_from=balances_from, credits=credits)
            loader.source_sha256 = meta['source_sha256']
            for step in ['departments', 'vendors', 'customers', 'items', 'receipts', 'carried_balances', 'orders', 'purchase_orders', 'vouchers']:
                if step == 'carried_balances' and loader.balances_from is None:
                    continue
                say(f'loading {step}')
                getattr(loader, step)()
                if step == 'carried_balances':
                    say(f"balances carried: {counts['balances_carried']} (${counts['balances_carried_total']:,}), dropped to 0: {counts['balances_dropped']} "
                        f"(${counts['balances_dropped_total']:,} incl. partial drops); credits carried: {counts['credits_carried']}, dropped: {counts['credits_dropped']}")
                else:
                    say(f'{step}: {counts.get(step, 0)}')
            held_con, held_path = held_source(con, path, meta)
            if held_con is None:
                say('held receipts: none in this dataset (no DataState-1 receipts and no held.sqlite beside it)')
                counts['held_receipts'] = 0
            else:
                say(f'loading held receipts from {held_path}')
                try:
                    loader.held_receipts(held_con)
                finally:
                    if held_con is not con:
                        held_con.close()
                say(f"held receipts: {counts['held_receipts']}")
            result['document_number_counters'] = reset_document_numbers(continue_qb_numbers)
            counts.update(relink_tag_lines())
            # every imported deposit's tax figures are fixed as of the load (reports.tax_ledger), so later
            # order edits never move a reported day. Same rule as `manage.py lock_order_payment_tax`.
            from reports.tax_ledger import lock_all_orders
            counts.update({f'order_payment_tax_{key}': value for key, value in lock_all_orders(say).items()})
            say(f"order payment tax locked: {counts['order_payment_tax_payments_locked']} payments on {counts['order_payment_tax_orders']} orders")
            # Items Trash: QB knows nothing of the app's trash, so every item came back active; re-trash by item number.
            from .trash import reapply
            counts.update(reapply(say))
            for table, expected in table_counts.items():
                counts[f'source_{table}'] = expected
            if not commit:
                say('dry run: rolling back')
                transaction.set_rollback(True)
            else:
                result['committed'] = True
    finally:
        con.close()
    result['seconds'] = round(time.monotonic() - started, 1)
    return result
