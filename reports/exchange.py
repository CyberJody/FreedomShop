# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local, reviewable daily exchange proposals. Never posts to a book or changes the ledger.

UUIDs in payloads identify local references, NOT existing FreedomBooks records. An importer must bind them and
choose a company before reviewing. Explicit POS tax is context outside Rust values, pending FreedomBooks support.
See docs/EXCHANGE.md for the envelope, accounting boundaries and blocked mappings.
"""
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid5
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from django.conf import settings
from django.utils import timezone

from purchasing.models import PurchaseDocument
from sales.models import Document, Line, RESTOCKING_FEE_NAME, money
from shop_settings.config import business, tax as shop_tax

from . import daily_totals, sales_tax, tax_ledger
from .tax_ledger import ZERO
from .tax_periods import Period

FORMAT = 'freedomshop-exchange/1'
MAX_RANGE_DAYS = 366
FIGURES = ('gross', 'discount', 'taxable', 'nontaxable', 'tax', 'total')


def parse_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        day = value
    else:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError('Use a date in YYYY-MM-DD format.')
    if day == date.max:
        raise ValueError('Choose a day before 9999-12-31.')
    return day


def days_between(start, end):
    start, end = parse_day(start), parse_day(end)
    if end < start or (end - start).days >= MAX_RANGE_DAYS:
        raise ValueError(f'Choose an ordered range of at most {MAX_RANGE_DAYS} days.')
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def amount(value):
    value = money(value)
    return '0.00' if not value else format(value, '.2f')


def _fb_method(method):
    return {'cash': 'CASH', 'check': 'CHECK', 'card': 'CARD'}.get(method, 'OTHER')


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def json_bytes(exchange):
    return canonical_bytes(exchange) + b'\n'


def request_key(shop_id, day, section, ordinal):
    return str(uuid5(UUID(str(shop_id)), f'{day}:{section}:{ordinal}'))


def split(value, weights):
    """Cumulative half-up allocation; final member receives the exact residual cent. Signed weights are allowed."""
    total = sum(weights, ZERO)
    if not total:
        return [ZERO] * len(weights)
    out, used, cumulative = [], ZERO, ZERO
    for weight in weights:
        cumulative += weight
        target = money(value * cumulative / total)
        out.append(target - used)
        used = target
    return out


def _source(ev):
    if ev.kind == 'order_payment':
        return f'{ev.order.numbered_label} payment {ev.source.pk}'
    return ev.doc.numbered_label


def _gross_share(ev, value):
    if ev.money_in - ev.payment == ev.new_sale:
        return value
    basis = money(ev.doc.subtotal or ZERO) - ev.poa_amount
    return money(value * tax_ledger.receipt_figures(ev)[0] / basis) if basis else ZERO


def _gift_redeemed(ev):
    if ev.kind == 'order_payment':
        return money(ev.source.amount) if ev.source.method == 'gift' else ZERO
    if ev.doc.from_quickbooks:
        tenders = [t for t in ev.doc.tenders.all() if not t.use_in_total]
        unknown = [t for t in tenders if t.amount is None]
        known = sum((money(t.amount) for t in tenders if t.amount is not None), ZERO)
        derived = money(ev.doc.total) - known if len(unknown) == 1 and ev.doc.total is not None else ZERO
        return sum((money(t.amount) if t.amount is not None else derived for t in tenders if t.method == 'gift'), ZERO)
    return sum((money(t['amount']) for t in ev.doc.payment_breakdown if t['method'] == 'gift'), ZERO)


def _rows(ev):
    """Project ledger figures independently of report layout, including zero-money, cross-location true-ups."""
    if ev.excluded == 'unlinked':
        return []
    location = shop_tax().section_of(ev.tax_location, ev.target.tax_exempt)
    rows = []
    if ev.is_deposit:
        gross = ev.money_in - ev.merchandise_tax
        rows.append((location, 'order', (gross, ZERO, ev.taxable, gross - ev.taxable,
                                         ev.merchandise_tax, ev.money_in)))
    elif not ev.poa:
        gross, discount, taxable, nontaxable = tax_ledger.receipt_figures(ev)
        rows.append((location, 'sale', (gross, discount, taxable, nontaxable,
                                        ev.merchandise_tax, ev.money_in - ev.payment)))
    for parts in (ev.allocations, ev.adjustments):
        for part in parts:
            rows.append((part.location, 'account', (part.amount - part.tax, ZERO, part.taxable,
                                                   part.nontaxable, part.tax, part.amount)))
    return rows


class Proposals:
    def __init__(self, shop_id, day):
        self.shop_id, self.day = shop_id, day
        self.commands, self.references = [], {}
        self.counts = defaultdict(int)

    def ref(self, kind, key, label):
        identifier = str(uuid5(UUID(str(self.shop_id)), f'reference:{kind}:{key}'))
        self.references[identifier] = dict(kind=kind, key=str(key), label=label)
        return identifier

    def add(self, section, command_type, values, sources, *, next_types=(), **context):
        ordinal = self.counts[section]
        self.counts[section] += 1
        key = request_key(self.shop_id, self.day, section, ordinal)
        if 'request_key' in values:
            values['request_key'] = key
        entry = dict(section=section, type=command_type, request_key=key, values=values,
                     fs_source=sorted(set(sources)), next_types=list(next_types), **context)
        self.commands.append(entry)
        return entry

    def invoice_line(self, description, value, taxable, department='Summary'):
        return dict(origin_position=None, item_id=self.ref('item', department, department), description=description,
                    quantity='1', rate=amount(value), taxable=taxable, refresh_item=False, keep_archived=False,
                    income_account_id=self.ref('account', f'income:{department}', f'Income: {department}'))

    def journal(self, section, value, debit, credit, sources, memo):
        if not value:
            return
        if value < 0:
            debit, credit, value = credit, debit, -value
        lines = [dict(account=self.ref('account', account, account), debit=amount(dr), credit=amount(cr),
                      description=memo, name='') for account, dr, cr in ((debit, value, ZERO), (credit, ZERO, value))]
        self.add(section, 'SaveJournal', dict(journal_id=None, date=self.day, memo=memo, entry_number='',
                                            adjusting=False, request_key='', expected_revision=None, lines=lines),
                 sources, next_types=('PostJournal',))


def _account_activity(ev):
    return ev.account or ev.payment or ev.credit or ev.adjustments or (ev.is_deposit and any(ev.tender.values()))


def _directory(proposals, events, vouchers):
    customers = {ev.target.customer_id: ev.target.customer for ev in events
                 if ev.target.customer_id and _account_activity(ev)}
    vendors = {v.vendor_id: v.vendor for v in vouchers if v.vendor_id}
    directory = dict(mode='summary', customers=[], vendors=[])
    for pk, customer in sorted(customers.items()):
        values = dict(kind='customer', display_name=customer.display_name, business_name=customer.company,
                      contact_name=customer.composed_name, email=customer.email, phone=customer.phone,
                      billing_address='\n'.join(customer.address_lines()), notes='',
                      tax_status='EXEMPT' if customer.tax_exempt else 'TAXABLE', exemption_number=None)
        record_id = proposals.ref('customer', pk, customer.display_name)
        sources = [_source(ev) for ev in events if ev.target.customer_id == pk and _account_activity(ev)]
        entry = proposals.add('customers', 'SaveDirectoryRecord', values, sources, record_id=record_id)
        directory['customers'].append(entry)
    for pk, vendor in sorted(vendors.items()):
        address = '\n'.join(p for p in (vendor.street, ' '.join(filter(None, (vendor.city, vendor.state, vendor.postal_code)))) if p)
        values = dict(kind='vendor', display_name=vendor.company, business_name=vendor.company, contact_name=vendor.contact,
                      email=vendor.email, phone=vendor.phone, billing_address=address, notes='',
                      default_expense_account=None, default_terms=vendor.terms or None, expense_shown=False, terms_shown=False)
        entry = proposals.add('vendors', 'SaveDirectoryRecord', values, [v.number for v in vouchers if v.vendor_id == pk],
                              record_id=proposals.ref('vendor', pk, vendor.company))
        directory['vendors'].append(entry)
    return directory


def _sale_commands(p, events, full_lines):
    groups = {}
    gift_sold, gift_sources = ZERO, []
    for ev in events:
        if ev.is_deposit or ev.poa:
            continue
        row = _rows(ev)[0][2]
        gross, discount, taxable, nontaxable, tax, total = row
        if not any(row):
            continue
        # Cash-basis gross share; gift issue is a liability, never a receipt income line.
        lines = full_lines.get(ev.doc.pk, [])
        gifts = sum((line.amount for line in lines if _is_gift(line)), ZERO)
        sold = _gross_share(ev, gifts)
        gift_sold += sold
        if sold:
            gift_sources.append(_source(ev))
        weights = [ev.tender[k] for k in ('cash', 'check', 'card', 'gift')]
        if not sum(weights, ZERO):
            # Nonzero bases/tax without a tender need a fiscal adjustment rather than a fabricated receipt.
            continue
        shares = [split(value, weights) for value in (gross - sold, discount, taxable, nontaxable - sold, tax, total - sold)]
        department_weights = defaultdict(lambda: ZERO)
        for line in lines:
            if line.item_name != tax_ledger.POA_NAME and not _is_gift(line):
                department = line.item.department.name if line.item_id and line.item.department_id else 'Unclassified'
                department_weights[(department, bool(line.tax_rate and line.tax_rate > 0 and not ev.doc.tax_exempt))] += line.net_amount
        # Separate positive and negative daily groups: refunds retain their ReviewCreditMemo / Refund workflow.
        for i, method in enumerate(('cash', 'check', 'card', 'gift')):
            figures = [parts[i] for parts in shares]
            # Keep both receipt identities for each tender. Independent rounding of all six columns can
            # leave a penny between the lines and the amount even when the daily columns tie perfectly.
            figures[0] = figures[5] - figures[4] + figures[1]
            figures[3] = figures[5] - figures[4] - figures[2]
            if not any(figures):
                continue
            location = shop_tax().section_of(ev.tax_location, ev.target.tax_exempt)
            key = (method, location, figures[-1] < 0)
            group = groups.setdefault(key, dict(figures=[ZERO] * 6, departments=defaultdict(lambda: ZERO),
                                               discounts=defaultdict(lambda: ZERO), sources=[]))
            group['figures'] = [a + b for a, b in zip(group['figures'], figures)]
            group['sources'].append(_source(ev))
            discount_parts = split(figures[1], [figures[2], figures[3]])
            if not figures[2] + figures[3]:
                discount_parts = [figures[1] if tax else ZERO, ZERO if tax else figures[1]]
            for taxable_flag, base, discount_part in zip((True, False), figures[2:4], discount_parts):
                departments = sorted(d for d in department_weights if d[1] == taxable_flag)
                weights_by_department = [department_weights[d] for d in departments]
                if not departments or not sum(weights_by_department, ZERO):
                    departments, weights_by_department = [('Unclassified', taxable_flag)], [money(1)]
                for department, portion in zip(departments, split(base + discount_part, weights_by_department)):
                    group['departments'][department] += portion
                group['discounts'][taxable_flag] += discount_part
    for (method, location, refund), group in sorted(groups.items()):
        gross, discount, taxable, nontaxable, tax, total = group['figures']
        sign = -1 if refund else 1
        lines = [p.invoice_line(dept, sign * value, taxable_flag, dept)
                 for (dept, taxable_flag), value in sorted(group['departments'].items()) if value]
        for taxable_flag, discount_part in sorted(group['discounts'].items()):
            if discount_part:
                lines.append(p.invoice_line('Discounts given', -sign * discount_part, taxable_flag, 'Discounts given'))
        customer = p.ref('customer', 'walk-in', 'Daily sales — walk-in')
        tax_item = p.ref('tax_item', location, location) if tax else None
        context = dict(expected_tax=amount(sign * tax), tax_location=location,
                       requires=['Bind destination book IDs and department items.', 'Accept stated POS tax; do not recompute.'])
        if refund:
            credit = p.add('returns', 'ReviewCreditMemo', dict(request_key='', customer_id=customer, date=p.day,
                           memo='Daily returns', tax_mode='ITEM' if tax else 'NONE', tax_item_id=tax_item, lines=lines),
                           group['sources'], next_types=('RecordCreditMemo',), **context)
            p.add('refunds', 'ReviewCustomerRefund', dict(request_key='', date=p.day, amount=amount(-total),
                  bank_id=p.ref('account', f'refund:{method}', f'Refund clearing: {method}'), method=_fb_method(method),
                  check_number='', memo='Daily refunds'), group['sources'], next_types=('RecordCustomerRefund',),
                  source_id=credit['request_key'], deposit=False)
        else:
            p.add('sales', 'ReviewCustomerReceipt', dict(request_key='', customer_id=customer, date=p.day,
                  method=_fb_method(method), reference=f'Freedom Shop {p.day}', memo=f'Daily sales ({method})', destination='UNDEPOSITED',
                  bank_id=None, amount=amount(total), tax_mode='ITEM' if tax else 'NONE', tax_item_id=tax_item, lines=lines, allocations=[]),
                  group['sources'], next_types=('RecordCustomerReceipt',), sales_receipt=True, **context)
    p.journal('gift_sold', gift_sold, 'Undeposited funds', 'Gift certificates outstanding', gift_sources, 'Gift certificates sold')


def _is_gift(line):
    return bool(line.certificate_number) or line.item_name.upper() == 'GIFT CERTIFICATE'


def _other_commands(p, events, receipts, vouchers):
    for ev in events:
        source = _source(ev)
        if ev.is_deposit and not ev.adjustment:
            for method in tax_ledger.TENDERS:
                value = ev.tender[method]
                if not value:
                    continue
                customer = p.ref('customer', ev.target.customer_id or 'walk-in', ev.target.customer_name or 'Daily sales — walk-in')
                order_ref = ev.order.number if ev.order else 'Unlinked deposit'
                requires = ['Resolve order deposit liability and POS cash-basis tax before posting.']
                if method == 'account':
                    requires.append('Account tender is a charge, not cash: requires receivable mapping.')
                if value < 0:
                    p.add('order_refunds', 'ReviewCustomerRefund', dict(request_key='', date=p.day, amount=amount(-value),
                          bank_id=p.ref('account', f'refund:{method}', f'Refund clearing: {method}'), method=_fb_method(method),
                          check_number='', memo=f'Order refund {order_ref}'), [source], next_types=('RecordCustomerRefund',),
                          source_id=p.ref('customer_deposit', ev.order.pk if ev.order else ev.doc.pk, order_ref), deposit=True,
                          requires=requires)
                else:
                    p.add('order_deposits', 'ReviewCustomerDeposit', dict(request_key='', customer_id=customer,
                          date=p.day, amount=amount(value), method=_fb_method(method), reference=source, destination='UNDEPOSITED',
                          bank_id=None, estimate_id=None, order_reference=order_ref, memo='Order deposit'), [source],
                          next_types=('RecordCustomerDeposit',), requires=requires)
        if ev.payment:
            methods = ('cash', 'check', 'card', 'gift')
            shares = split(ev.payment, [ev.tender[method] for method in methods])
            for method, value in zip(methods, shares):
                if value < 0:
                    p.add('account_refunds', 'ReviewCustomerRefund', dict(request_key='', date=p.day, amount=amount(-value),
                          bank_id=p.ref('account', f'refund:{method}', f'Refund clearing: {method}'), method=_fb_method(method),
                          check_number='', memo='Payment on account refunded'), [source], next_types=('RecordCustomerRefund',),
                          source_id=p.ref('account_credit', ev.target.customer_id or 'walk-in', ev.target.customer_name),
                          deposit=False, requires=['Resolve the historical credit or prepayment lots before refunding.'])
                elif value > 0:
                    p.add('payments_on_account', 'ReviewCustomerReceipt', dict(request_key='',
                          customer_id=p.ref('customer', ev.target.customer_id or 'walk-in', ev.target.customer_name), date=p.day,
                          method=_fb_method(method), reference=source, memo='Payment on account', destination='UNDEPOSITED', bank_id=None,
                          amount=amount(value), tax_mode=None, tax_item_id=None, lines=[], allocations=[]), [source],
                          next_types=('RecordCustomerReceipt',), sales_receipt=False,
                          requires=['Summary mode: bind customer and historical invoice allocations before posting.'])
    for receipt in receipts:
        if receipt.source_order_id and not receipt.from_quickbooks:
            p.add('deposits_applied', 'ReviewDepositApplication', dict(request_key='', date=p.day,
                  amount=amount(receipt.total or ZERO), invoice_id=p.ref('invoice', receipt.pk, receipt.numbered_label)),
                  [receipt.numbered_label], next_types=('RecordDepositApplication',),
                  source_id=p.ref('customer_deposit', receipt.source_order_id, receipt.source_order.numbered_label),
                  requires=['Resolve pickup invoice and deposit lots; do not recognize previously taxed sales twice.'])
    gift = sum((_gift_redeemed(ev) for ev in events), ZERO)
    p.journal('gift_redeemed', gift, 'Gift certificates outstanding', 'Undeposited funds',
              [_source(ev) for ev in events if ev.gift], 'Gift/other tender redeemed; verify certificate provenance')
    for voucher in vouchers:
        lines = [dict(origin_position=None, item_id=None,
                      expense_account_id=p.ref('account', 'Inventory', 'Inventory'), description=line.description or line.item_name,
                      quantity='1', rate=amount(line.amount), refresh_item=False, keep_archived=False)
                 for line in voucher.lines.all()]
        # Keep the posted voucher total, including freight and imported line residuals, exactly.
        residual = money(voucher.total) - sum((line.amount for line in voucher.lines.all()), ZERO)
        if residual:
            lines.append(dict(origin_position=None, item_id=None, expense_account_id=p.ref('account', 'Receiving adjustments',
                              'Receiving adjustments'), description='Freight / recorded voucher residual', quantity='1',
                              rate=amount(residual), refresh_item=False, keep_archived=False))
        p.add('receiving', 'SaveBill', dict(bill_id=None, expected_revision=0, request_key='',
              vendor_id=p.ref('vendor', voucher.vendor_id or f'missing:{voucher.pk}', voucher.vendor_name),
              date=(voucher.invoice_date.isoformat() if voucher.invoice_date else p.day),
              due_date=(voucher.payment_due.isoformat() if voucher.payment_due else p.day), memo=voucher.notes,
              reference=voucher.invoice_number or voucher.number, supersedes_id=None, reentry_reason='', refresh_vendor=False,
              keep_archived=False, lines=lines), [voucher.number], next_types=('ReviewBill', 'PostBill'),
              requires=['Bind vendor and inventory accounts; confirm invoice and due dates.'])


def _bank_deposit_commands(p):
    groups = defaultdict(list)
    for entry in p.commands:
        if entry['type'] in ('ReviewCustomerReceipt', 'ReviewCustomerDeposit'):
            values = entry['values']
            if values['method'] in ('CASH', 'CHECK', 'CARD') and money(values['amount']) > 0:
                groups[values['method']].append(entry)
    for method, entries in sorted(groups.items()):
        p.add('bank_deposits', 'ReviewDeposit', dict(request_key='', bank_id=p.ref('bank', method, f'Bank for {method}'),
              date=p.day, reference=f'Freedom Shop {p.day} {method}', memo='Optional bank deposit',
              receipt_ids=[entry['request_key'] for entry in entries]),
              [source for entry in entries for source in entry['fs_source']], next_types=('RecordDeposit',), optional=True,
              requires=['Optional: resolve recorded receipt IDs and bank; omit if deposits are entered manually.',
                        'Reconcile gifts sold, refunds, fees and actual bank settlements before selecting receipts.'])


def _department_commands(p):
    for identifier, ref in sorted(list(p.references.items()), key=lambda pair: pair[1]['key']):
        if ref['kind'] != 'item':
            continue
        sources = [source for entry in p.commands if any(line.get('item_id') == identifier
                   for line in entry['values'].get('lines', [])) for source in entry['fs_source']]
        p.add('items', 'SaveDirectoryRecord', dict(kind='item', code=f'FS-{identifier[:12]}',
              description=ref['label'], item_kind='NONSTOCK', sale_rate='0.00', purchase_cost=None,
              income_account=p.ref('account', f'income:{ref["key"]}', f'Income: {ref["label"]}'),
              expense_account=None, taxable=None), sources, record_id=identifier,
              requires=['Bind department items and income accounts; reuse the same mapping on subsequent days.'])
    # Directory proposals precede the documents that refer to them. Keys remain scoped to their own section.
    p.commands.sort(key=lambda entry: entry['type'] != 'SaveDirectoryRecord')


def _build_day(day):
    day = parse_day(day)
    shop = business()
    if shop.pk is None:
        raise ValueError('Save Business Settings before exporting; the shop identity must be persisted.')
    period = Period('custom', day, day, day.isoformat())
    daily = daily_totals.build(period)
    walk = daily.walk
    events = sorted(walk.events, key=lambda ev: (ev.when, ev.kind == 'order_payment', ev.source.pk if ev.source else ev.doc.pk))
    lower, upper = tax_ledger.bounds(day, day)
    receipts = list(Document.objects.filter(kind='sale', status='completed', completed_at__gte=lower,
                                           completed_at__lt=upper).select_related('source_order').order_by('completed_at', 'pk'))
    full_lines = defaultdict(list)
    for line in Line.objects.filter(document_id__in=[r.pk for r in receipts]).select_related('item__department').order_by('document_id', 'position', 'pk'):
        full_lines[line.document_id].append(line)
    vouchers = list(PurchaseDocument.objects.filter(kind='voucher', status='posted', posted_at__gte=lower, posted_at__lt=upper)
                    .select_related('vendor').prefetch_related('lines').order_by('posted_at', 'pk'))
    locations = {loc.name: dict.fromkeys(FIGURES, ZERO) for loc in shop_tax().locations}
    for ev in events:
        for location, kind, figures in _rows(ev):
            bucket = locations.setdefault(location, dict.fromkeys(FIGURES, ZERO))
            for key, value in zip(FIGURES, figures):
                bucket[key] += value
    totals = {key: sum((bucket[key] for bucket in locations.values()), ZERO) for key in FIGURES}
    tender_totals = {key: sum((ev.tender[key] for ev in events), ZERO) for key in tax_ledger.TENDERS}
    received = sum((ev.received for ev in events), ZERO)
    unlinked = sum((ev.money_in for ev in events if ev.excluded == 'unlinked'), ZERO)
    p = Proposals(shop.exchange_id, day.isoformat())
    directory = _directory(p, events, vouchers)
    _sale_commands(p, events, full_lines)
    _other_commands(p, events, receipts, vouchers)
    cost, missing_costs = ZERO, 0
    for receipt in receipts:
        if receipt.receipt_type in (2, 3, 4):
            continue
        for line in full_lines[receipt.pk]:
            if _is_gift(line) or line.restocking_fee or line.item_name in (tax_ledger.POA_NAME, RESTOCKING_FEE_NAME):
                continue
            if line.item_id and line.item.item_type != 'inventory':
                continue
            if line.quantity < 0 and line.damaged:
                continue  # No sellable inventory came back; its cost remains expensed.
            if line.unit_cost is None:
                missing_costs += 1
            else:
                cost += money(line.quantity * line.unit_cost)
    p.journal('cogs', cost, 'Cost of goods sold', 'Inventory', [r.numbered_label for r in receipts
              if r.receipt_type not in (2, 3, 4)], 'Known recorded cost of goods sold; missing costs reported separately')
    _bank_deposit_commands(p)
    _department_commands(p)
    deposits = sum((sum(ev.tender.values(), ZERO) for ev in events if ev.is_deposit), ZERO)
    applied = sum((money(r.total or ZERO) for r in receipts if r.source_order_id and not r.from_quickbooks), ZERO)
    gift_sold = restocking = returned = ZERO
    for ev in events:
        if ev.is_deposit or ev.poa:
            continue
        for line in full_lines[ev.doc.pk]:
            share = _gross_share(ev, line.amount)
            if _is_gift(line):
                gift_sold += share
            if line.restocking_fee or line.item_name == RESTOCKING_FEE_NAME:
                restocking += share
            if line.amount < 0 and line.item_name != tax_ledger.POA_NAME:
                returned -= share
    summary = dict(gross_sales=amount(totals['gross']), discounts=amount(totals['discount']), returns=amount(returned),
                   taxable=amount(totals['taxable']), nontaxable=amount(totals['nontaxable']),
                   tax_collected={loc: amount(values['tax']) for loc, values in sorted(locations.items())},
                   by_tax_location={loc: {k: amount(v) for k, v in values.items()} for loc, values in sorted(locations.items())},
                   tenders={k: amount(v) for k, v in tender_totals.items()}, received=amount(received),
                   total=amount(totals['total']), tender_total=amount(sum(tender_totals.values(), ZERO)),
                   unlinked_deposits=amount(unlinked), order_deposits_taken=amount(deposits),
                   order_deposits_applied=amount(applied), legacy_deposits_excluded=amount(walk.applied_total),
                   payments_on_account=amount(walk.poa_total), gift_certificates_sold=amount(gift_sold),
                   gift_certificates_redeemed=amount(sum((_gift_redeemed(ev) for ev in events), ZERO)),
                   other_tenders=amount(sum((ev.gift - _gift_redeemed(ev) for ev in events), ZERO)), restocking_fees=amount(restocking),
                   cogs=amount(cost), missing_cost_lines=missing_costs,
                   inventory_received_at_cost=amount(sum((money(v.total) for v in vouchers), ZERO)),
                   daily_totals={k: amount(v) for k, v in daily.totals.items()},
                   daily_sales_summary={row['label']: amount(row['amount']) for row in daily.summary})
    st = sales_tax.build(period)
    st_locations = {section.location: section for section in st.sections}
    tender_ok = not walk.excluded['missing'] and all(
        sum(ev.tender.values(), ZERO) == money(ev.source.amount if ev.kind == 'order_payment' else ev.doc.total or ZERO)
        for ev in events)
    daily_ok = (totals['total'] + unlinked == received + tender_totals['gift'] and
                received == daily.totals['received'] and sum(ev.tax for ev in events) == daily.totals['tax']
                and totals['tax'] == daily.totals['tax'] and
                all(tender_totals[k] == daily.totals[k] for k in tax_ledger.TENDERS))
    tax_ok = (all(totals[k] == getattr(st, k) for k in FIGURES) and all(
        all(value == getattr(st_locations[loc], key) if loc in st_locations else value == ZERO
            for key, value in values.items()) for loc, values in locations.items()))
    body = dict(format=FORMAT, shop=dict(id=str(shop.exchange_id), name=shop.name,
                currency=getattr(settings, 'SHOP_CURRENCY', 'USD'), time_zone=timezone.get_current_timezone_name()),
                day=day.isoformat(), source=dict(app='freedomshop', version='1', basis='cash'), summary=summary,
                directory=directory, commands=p.commands, references=dict(sorted(p.references.items())),
                ledger=[dict(fs_source=_source(ev), location=location, kind=kind,
                             **{key: amount(value) for key, value in zip(FIGURES, figures)})
                        for ev in events for location, kind, figures in _rows(ev)],
                checks=dict(tenders_equal_total=bool(tender_ok), ledger_ties_daily_totals=bool(daily_ok), ledger_ties_sales_tax=bool(tax_ok)),
                diagnostics=dict(excluded={k: walk.excluded[k] for k in sorted(walk.excluded)},
                                 estimated_payments=walk.estimated_count + walk.exempt_estimated_count,
                                 estimated_total=amount(walk.estimated_total), estimated_tax=amount(walk.estimated_tax),
                                 tax_adjustments=amount(walk.reconciled_tax)),
                requires=['Import proposals only: choose the company and bind local UUID references before review.',
                          'POS tax amounts are authoritative; tax overrides and summary import need FreedomBooks support.',
                          'Corrections are dated command sets; exported files must never be replaced with changed bodies.'])
    return dict(body=body, body_sha256=hashlib.sha256(canonical_bytes(body)).hexdigest(),
                generated_at=timezone.now().isoformat())


def build_day(day):
    from .models import ExchangeRun
    day = parse_day(day)
    run = ExchangeRun.objects.filter(day=day, exported_at__isnull=False).first()
    if run:
        return json.loads(bytes(run.payload))
    result = _build_day(day)
    return include_corrections(result)


def include_corrections(result):
    from .exchange_corrections import apply_corrections
    apply_corrections(result['body'])
    result['body_sha256'] = hashlib.sha256(canonical_bytes(result['body'])).hexdigest()
    return result


def zip_bytes(start, end):
    from .exchange_sync import export_day, outbox_path
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for day in days_between(start, end):
            info = ZipInfo(f'freedomshop-exchange-{day.isoformat()}.json', date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            run = export_day(day, outbox_path() if business().exchange_outbox else None)
            archive.writestr(info, bytes(run.payload))
    return output.getvalue()
