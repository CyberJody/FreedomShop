# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import functools
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4, uuid5

from django.test import TestCase

try:  # test-only dependency (requirements-test.txt); the tests that validate against the schema skip without it
    import jsonschema
except ImportError:
    jsonschema = None

from customers.models import Customer
from purchasing.models import Department, PurchaseDocument, PurchaseLine, Vendor
from sales.models import Document, Item, OrderPayment, Tender
from shop_settings.config import business, clear_cache
from shop_settings.forms import BusinessForm
from shop_settings.models import Business, TaxLocation

from . import daily_totals, exchange, sales_tax
from .tax_periods import Period
from .test_tax_ledger import LedgerFixture, WHEN

DAY = WHEN.date()
SCHEMA = json.loads(Path(__file__).with_name('exchange_schema.json').read_text())


@functools.cache
def _validator():
    return jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())


def validator(test):
    """The Draft 2020-12 validator for the shipped schema; skips the calling test when jsonschema isn't installed."""
    if jsonschema is None:
        test.skipTest('jsonschema is not installed: pip install -r requirements-test.txt')
    return _validator()


class ExchangeAssertions:
    def assert_ties(self, day=DAY):
        result = exchange.build_day(day)
        validator(self).validate(result)
        body, summary = result['body'], result['body']['summary']
        period = Period('custom', day, day, str(day))
        daily, tax = daily_totals.build(period), sales_tax.build(period)
        self.assertEqual(summary['daily_totals'], {k: exchange.amount(v) for k, v in daily.totals.items()})
        self.assertEqual(summary['daily_sales_summary'], {r['label']: exchange.amount(r['amount']) for r in daily.summary})
        for exported, field in [('gross_sales', 'gross'), ('discounts', 'discount'), ('taxable', 'taxable'),
                                ('nontaxable', 'nontaxable'), ('total', 'total')]:
            self.assertEqual(D(summary[exported]), getattr(tax, field))
        self.assertEqual(sum(map(D, summary['tax_collected'].values())), daily.totals['tax'])
        for section in tax.sections:
            self.assertEqual(summary['by_tax_location'][section.location],
                             {k: exchange.amount(getattr(section, k)) for k in exchange.FIGURES})
        self.assertEqual(D(summary['received']), daily.totals['received'])
        self.assertEqual(D(summary['total']) + D(summary['unlinked_deposits']),
                         D(summary['received']) + D(summary['tenders']['gift']))
        self.assertTrue(all(body['checks'].values()), body['checks'])
        self.assertEqual(result['body_sha256'], hashlib.sha256(exchange.canonical_bytes(body)).hexdigest())
        return body


class ExchangeLedgerTests(ExchangeAssertions, LedgerFixture):
    def test_existing_daily_totals_scenario(self):
        # Reuse the report fixture covering mixed tender, partial account, order deposits, exempt and reversal.
        from .test_daily_totals import DailyTotalsTests
        fixture = DailyTotalsTests('test_all_columns_and_by_day_totals')
        fixture.setUp()
        fixture.test_all_columns_and_by_day_totals()
        body = self.assert_ties()
        self.assertEqual(body['summary']['order_deposits_taken'], '321.00')
        self.assertEqual(body['summary']['payments_on_account'], '57.00')
        self.assertEqual(body['summary']['gift_certificates_redeemed'], '107.00')

    def test_existing_sales_tax_fixture_each_day(self):
        from .test_sales_tax import SalesTaxReportTests
        SalesTaxReportTests.setUpTestData()
        for offset in range(30):
            with self.subTest(day=offset + 1):
                self.assert_ties(date(2026, 9, 1) + timedelta(days=offset))

    def test_fifo_final_cent_cross_location_and_credit_true_up(self):
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=2))
        self.doc(total='100', tax='0', payments=(('account', '100'),), tax_location='Exempt', tax_exempt=True,
                 when=WHEN - timedelta(days=1))
        self.doc(total='207', tax='0', payments=(('check', '207'),), poa=True)
        self.assert_ties()
        self.doc(total='107', tax='0', poa=True, when=WHEN + timedelta(days=1))
        self.doc(total='100', tax='0', payments=(('account', '100'),), tax_exempt=True, tax_location='Exempt',
                 when=WHEN + timedelta(days=2))
        body = self.assert_ties(DAY + timedelta(days=2))
        self.assertNotEqual(body['diagnostics']['tax_adjustments'], '0.00')
        self.assertTrue(any(r['kind'] == 'account' for r in body['ledger']))

    def test_fractional_prices_discount_and_split_tenders_preserve_pennies(self):
        doc = self.doc(total='97.70', tax='3.70', payments=(('cash', '20.01'), ('credit', '30.02'), ('gift', '47.67')),
                       lines=[('Chair', '50.00', 7), ('Finish', '50.00', 0)])
        Document.objects.filter(pk=doc.pk).update(subtotal=D('100.00'), discount=D('6.00'))
        body = self.assert_ties()
        receipts = [c for c in body['commands'] if c['type'] == 'ReviewCustomerReceipt' and c.get('sales_receipt')]
        self.assertEqual(len(receipts), 3)
        self.assertEqual(sum((D(c['values']['amount']) for c in receipts)), D('97.70'))
        self.assertEqual(sum((D(c['expected_tax']) for c in receipts)), D('3.70'))
        for entry in receipts:
            values = entry['values']
            self.assertEqual(sum(D(line['rate']) for line in values['lines']) + D(entry['expected_tax']), D(values['amount']))
            self.assertTrue(any(not line['taxable'] for line in values['lines']))
            self.assertTrue(any(D(line['rate']) < 0 for line in values['lines']))

    def test_deposits_pickups_vouchers_directory_and_command_shapes(self):
        department = Department.objects.create(name='Seating')
        item = Item.objects.create(sku='DEMO-1', name='Demo chair', description='Chair', department=department)
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))
        self.doc(total='107', tax='0', payments=(('check', '107'),), poa=True)
        order = self.doc(order=True, when=WHEN - timedelta(days=1))
        OrderPayment.objects.create(order=order, amount='107', method='cash', created_at=WHEN)
        pickup = self.doc(source_order=order, total='107', tax='7')
        pickup.lines.update(item=item, unit_cost='40')
        sale = self.doc(payments=(('credit', '107'),))
        sale.lines.update(item=item, unit_cost='40')
        vendor = Vendor.objects.create(company='Demo Supply Co.', code='DEMO', street='123 Main St', city='Springfield')
        unused_vendor = Vendor.objects.create(company='Unused Demo Supplier')
        voucher = PurchaseDocument.objects.create(kind='voucher', status='posted', vendor=vendor, vendor_name=vendor.company,
                                                  total='45.03', posted_at=WHEN, freight='5.03')
        PurchaseLine.objects.create(document=voucher, position=1, description='Chair', item=item, quantity=1, cost='40')
        body = self.assert_ties()
        self.assertEqual([c['values']['display_name'] for c in body['directory']['vendors']], [vendor.company])
        self.assertEqual(len(body['directory']['customers']), 1)
        self.assertEqual(body['summary']['order_deposits_applied'], '107.00')
        self.assertEqual(body['summary']['inventory_received_at_cost'], '45.03')
        self.assertEqual(body['summary']['cogs'], '80.00')
        types = {entry['type'] for entry in body['commands']}
        self.assertTrue({'ReviewCustomerReceipt', 'ReviewCustomerDeposit', 'ReviewDepositApplication',
                         'SaveBill', 'SaveDirectoryRecord', 'SaveJournal', 'ReviewDeposit'} <= types)
        bill = next(c for c in body['commands'] if c['type'] == 'SaveBill')
        self.assertEqual(sum(D(line['rate']) for line in bill['values']['lines']), D('45.03'))
        self.assertEqual(bill['next_types'], ['ReviewBill', 'PostBill'])
        self.assertEqual(bill['fs_source'], [voucher.number])
        self.assertNotIn(unused_vendor.company, json.dumps(body))
        for entry in body['commands']:
            if entry['type'].startswith('Review'):
                self.assertTrue(entry['next_types'][0].startswith('Record'))
            self.assertNotIn('company_id', entry)
        sales = next(c for c in body['commands'] if c['section'] == 'sales')
        self.assertTrue(any(line['description'] == 'Seating' for line in sales['values']['lines']))
        items = {c['record_id']: c['values'] for c in body['commands'] if c['section'] == 'items'}
        for line in sales['values']['lines']:
            self.assertEqual(items[line['item_id']]['income_account'], line['income_account_id'])

    def test_missing_and_mismatched_tenders_fail_instead_of_inventing_money(self):
        self.doc(imported=True, payments=())
        body = exchange.build_day(DAY)['body']
        self.assertFalse(body['checks']['tenders_equal_total'])
        self.assertEqual(body['summary']['received'], '0.00')
        self.doc(total='107', payments=(('cash', '50'),))
        self.assertFalse(exchange.build_day(DAY)['body']['checks']['tenders_equal_total'])

    def test_other_tenders_do_not_become_gift_liability(self):
        self.doc(payments=(('other', '107'),))
        body = self.assert_ties()
        self.assertEqual(body['summary']['gift_certificates_redeemed'], '0.00')
        self.assertEqual(body['summary']['other_tenders'], '107.00')
        self.assertFalse(any(c['section'] == 'gift_redeemed' for c in body['commands']))

    def test_inferred_gift_tender_matches_the_ledger_without_becoming_other(self):
        receipt = self.doc(imported=True, payments=(('cash', '50'),))
        Tender.objects.create(document=receipt, position=2, method='gift', amount=None)
        body = self.assert_ties()
        self.assertEqual(body['summary']['gift_certificates_redeemed'], '57.00')
        self.assertEqual(body['summary']['other_tenders'], '0.00')
        journal = next(c for c in body['commands'] if c['section'] == 'gift_redeemed')
        self.assertEqual(journal['values']['lines'][0]['debit'], '57.00')

    def test_account_payments_keep_split_tenders_and_refunds_use_refund_workflow(self):
        self.doc(payments=(('account', '107'),), when=WHEN - timedelta(days=1))
        payment = self.doc(total='107', tax='0', payments=(('cash', '50'), ('debit', '57')), poa=True)
        body = self.assert_ties()
        proposals = [c for c in body['commands'] if c['section'] == 'payments_on_account']
        self.assertEqual([(c['values']['method'], c['values']['amount']) for c in proposals],
                         [('CASH', '50.00'), ('CARD', '57.00')])
        self.assertTrue(all(c['fs_source'] == [payment.numbered_label] for c in proposals))
        self.doc(total='-107', tax='0', payments=(('cash', '-107'),), lines=[('Payment On Account', '-107', 0)],
                 imported=True, when=WHEN + timedelta(days=1))
        refunded = self.assert_ties(DAY + timedelta(days=1))
        refund = next(c for c in refunded['commands'] if c['section'] == 'account_refunds')
        self.assertEqual(refund['type'], 'ReviewCustomerRefund')
        self.assertEqual(refund['values']['amount'], '107.00')
        self.assertFalse(any(c['type'] == 'ReviewCustomerReceipt' for c in refunded['commands']))

    def test_summary_directory_includes_deposit_holder_but_omits_ordinary_buyer(self):
        self.doc()
        holder = Customer.objects.create(first_name='Demo', last_name='Depositor')
        order = self.doc(order=True, customer=holder, when=WHEN - timedelta(days=1))
        OrderPayment.objects.create(order=order, amount='107', method='cash', created_at=WHEN)
        body = self.assert_ties()
        self.assertEqual([c['values']['display_name'] for c in body['directory']['customers']], [holder.display_name])
        deposit = next(c for c in body['commands'] if c['type'] == 'ReviewCustomerDeposit')
        self.assertEqual(deposit['values']['customer_id'], body['directory']['customers'][0]['record_id'])

    def test_noninventory_costs_do_not_credit_inventory(self):
        service = Item.objects.create(sku='DEMO-SERVICE', name='Demo service', item_type='noninventory')
        sale = self.doc()
        sale.lines.update(item=service, unit_cost='40')
        body = self.assert_ties()
        self.assertEqual(body['summary']['cogs'], '0.00')
        self.assertEqual(body['summary']['missing_cost_lines'], 0)
        self.assertFalse(any(c['section'] == 'cogs' for c in body['commands']))

    def test_empty_day_determinism_uuid_keys_and_persisted_identity(self):
        empty = self.assert_ties()
        self.assertEqual(empty['commands'], [])
        self.assertEqual(empty['summary']['gross_sales'], '0.00')
        self.doc()
        first = exchange.build_day(DAY)
        clear_cache()
        second = exchange.build_day(DAY)
        self.assertEqual(exchange.canonical_bytes({k: v for k, v in first.items() if k != 'generated_at'}),
                         exchange.canonical_bytes({k: v for k, v in second.items() if k != 'generated_at'}))
        with mock.patch('reports.exchange.timezone.now', return_value=WHEN):
            self.assertEqual(exchange.json_bytes(exchange.build_day(DAY)), exchange.json_bytes(exchange.build_day(DAY)))
        counters = {}
        for entry in first['body']['commands']:
            ordinal = counters.get(entry['section'], 0)
            expected = str(uuid5(UUID(first['body']['shop']['id']), f'{DAY}:{entry["section"]}:{ordinal}'))
            self.assertEqual(entry['request_key'], expected)
            counters[entry['section']] = ordinal + 1
            if 'request_key' in entry['values']:
                self.assertEqual(entry['values']['request_key'], expected)
        identity = business().exchange_id
        row = Business.objects.get(pk=business().pk)
        row.name = 'Demo Home & Hardware'
        row.save()
        self.assertEqual(business().exchange_id, identity)
        self.assertNotIn('exchange_id', BusinessForm().fields)
        row.exchange_id = uuid4()
        row.save()
        self.assertNotEqual(exchange.build_day(DAY)['body']['commands'][0]['request_key'], first['body']['commands'][0]['request_key'])

    def test_retired_tax_location_and_local_day_boundary(self):
        TaxLocation.objects.create(name='Lakeside', rate=5, active=False)
        self.doc(total='105', tax='5', payments=(('cash', '105'),), tax_location='Lakeside', when=WHEN.replace(hour=23, minute=59))
        self.doc(total='107', when=WHEN + timedelta(days=1))
        body = self.assert_ties()
        self.assertEqual(body['summary']['by_tax_location']['Lakeside']['tax'], '5.00')
        self.assertEqual(body['summary']['received'], '105.00')

    def test_schema_rejects_extra_rust_fields_floats_invalid_uuid_date_and_format(self):
        self.doc()
        valid = exchange.build_day(DAY)
        validator(self).check_schema(SCHEMA)
        changes = [('body.summary.received', 107.0), ('body.shop.id', 'broken'), ('body.day', '2026-02-30'),
                   ('body.format', 'other/2'), ('body.commands.0.values.tax_amount', '7.00')]
        for path, value in changes:
            invalid = copy.deepcopy(valid)
            target = invalid
            parts = path.split('.')
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            target[parts[-1]] = value
            with self.subTest(path=path), self.assertRaises(jsonschema.ValidationError):
                validator(self).validate(invalid)


class ExchangeRegisterTests(ExchangeAssertions, TestCase):
    def test_real_discounted_return_exchange_restocking_and_gift_paths(self):
        from sales.test_returns import ReturnTestCase, SALE_DAY
        fixture = ReturnTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        sale = fixture.save([dict(item_id=fixture.chair.pk, quantity='2', price='100')],
                            [dict(method='cash', amount='192.60')], discount_percent='10', discount_reason='Sale')
        returned = fixture.save([fixture.returned(sale), dict(item_id=fixture.lamp.pk, quantity='1', price='50')],
                                [dict(method='cash', amount='-23.54')], restocking_fee=True)
        gift = fixture.save([dict(item_name='GIFT CERTIFICATE', description='Demo gift', certificate_number='DEMO-GIFT',
                                  quantity='1', price='107', tax_rate='0')], [dict(method='cash', amount='107')])
        redeemed = fixture.sell('1', payments=[dict(method='gift', amount='107', reference='DEMO-GIFT')])
        Document.objects.filter(pk__in=[sale.pk, returned.pk, gift.pk, redeemed.pk]).update(completed_at=SALE_DAY)
        body = self.assert_ties(SALE_DAY.date())
        self.assertEqual(body['summary']['gift_certificates_sold'], '107.00')
        self.assertEqual(body['summary']['gift_certificates_redeemed'], '107.00')
        self.assertEqual(body['summary']['restocking_fees'], '18.00')
        self.assertEqual(body['summary']['returns'], '100.00')
        self.assertTrue({'ReviewCreditMemo', 'ReviewCustomerRefund'} <= {c['type'] for c in body['commands']})
        for section in ('gift_sold', 'gift_redeemed', 'cogs'):
            journal = next(c for c in body['commands'] if c['section'] == section)
            self.assertEqual(sum(D(l['debit']) for l in journal['values']['lines']),
                             sum(D(l['credit']) for l in journal['values']['lines']))
