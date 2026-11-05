# SPDX-License-Identifier: AGPL-3.0-or-later
"""no fractions of a cent anywhere. Round half-up to whole cents."""
import uuid
from decimal import Decimal

from django.test import TestCase

from customers.models import Customer
from purchasing.models import ItemVendor, Vendor
from .models import Item, money, plain, tax_amount
from .printing import price_text
from .services import save_document


class WholeCentsTests(TestCase):
    def test_money_rounds_half_up(self):
        self.assertEqual(money(Decimal('106.475')), Decimal('106.48'))   # receipt #121049 (QB Sale price)
        self.assertEqual(money(Decimal('138.7125')), Decimal('138.71'))  # receipt #121326
        self.assertEqual(money(Decimal('-14.955')), Decimal('-14.96'))
        self.assertEqual((plain(Decimal('247.40333')), price_text(Decimal('106.475'))), ('247.40', '$106.48'))

    def test_tax_is_figured_per_rate_on_the_subtotal_like_qb(self):
        # Lysa Ladouceur's QB held receipt: 5 lines, subtotal $10,239.75, QB tax $716.78. Line by line gave $716.80.
        amounts = [Decimal(a) for a in ('2084.95', '2179.95', '3274.95', '1449.95', '1249.95')]
        self.assertEqual(sum(amounts), Decimal('10239.75'))
        self.assertEqual(tax_amount((a, Decimal('7')) for a in amounts), Decimal('716.78'))
        self.assertEqual(tax_amount([(Decimal('100'), Decimal('7')), (Decimal('50'), Decimal('0'))]), Decimal('7.00'))
        self.assertEqual(tax_amount([(Decimal('100'), Decimal('7'))], exempt=True), Decimal('0.00'))

    def test_money_fields_round_half_up_on_every_save(self):
        item = Item.objects.create(sku='R8', name='R8 TEST', description='x', price='0.999', price_4='89.123', average_cost='601.234567', order_cost='701.345678', tax_rate=7)
        item.refresh_from_db()
        self.assertEqual((item.price, item.price_4, item.average_cost, item.order_cost), (Decimal('1.00'), Decimal('89.12'), Decimal('601.23'), Decimal('701.35')))
        Item.objects.filter(pk=item.pk).update(price=Decimal('5.559'))
        self.assertEqual(Item.objects.get(pk=item.pk).price, Decimal('5.56'))
        vendor = Vendor.objects.create(code='V', company='V')
        ItemVendor.objects.create(item=item, vendor=vendor, order_cost='650.125')
        self.assertEqual(ItemVendor.objects.get(vendor=vendor).order_cost, Decimal('650.13'))

    def test_register_sale_uses_half_up_extensions_and_tax(self):
        # Quantities are whole numbers, so a line extension is always whole cents; the tax is what gets rounded half-up.
        customer = Customer.objects.create(source_id='c', full_name='WHOLE CENTS')
        item = Item.objects.create(sku='T', name='T', description='Table', price='1899.95', on_hand=5, tax_rate='7.000')
        token = str(uuid.uuid4())
        doc = save_document(dict(token=token, version=0, action='hold', customer_id=customer.pk,
                                 lines=[dict(item_id=item.pk, quantity='1', price='1899.95', tax_rate='7'),
                                        dict(item_id=item.pk, quantity='3', price='464.95', tax_rate='7')]), 'sale', token)
        lines = list(doc.lines.order_by('position'))
        self.assertEqual([l.amount for l in lines], [Decimal('1899.95'), Decimal('1394.85')])
        self.assertEqual((doc.subtotal, doc.tax, doc.total), (Decimal('3294.80'), Decimal('230.64'), Decimal('3525.44')))  # 230.636 rounded half-up


class RecordedLineAmountTests(TestCase):
    """Old QB receipts still add up: QB's own line total is kept where its price had fractions of a cent."""

    def test_qb_line_amount_only_when_whole_cents_would_not_match(self):
        from .models import qb_line_amount
        self.assertEqual(qb_line_amount(Decimal('2'), Decimal('138.7125'), Decimal('138.71')), Decimal('277.43'))  # #121326 (whole-cent store)
        self.assertIsNone(qb_line_amount(Decimal('2'), Decimal('138.7125'), Decimal('138.7125')))  # 4-decimal store reproduces it
        self.assertEqual(qb_line_amount(Decimal('2'), Decimal('2.00249'), Decimal('2.0025')), Decimal('4.00'))  # 5 decimals: 4.00498 vs 4.0050
        self.assertIsNone(qb_line_amount(Decimal('1'), Decimal('138.7125'), Decimal('138.71')))  # 138.71 either way
        self.assertIsNone(qb_line_amount(Decimal('3'), Decimal('99.95'), Decimal('99.95')))

    def test_line_total_uses_qb_amount_and_app_edits_clear_it(self):
        from .models import Document, Line
        customer = Customer.objects.create(source_id='c', full_name='OLD RECEIPT')
        doc = Document.objects.create(kind='sale', status='held', customer=customer, customer_name='OLD RECEIPT', subtotal='277.43', tax='0', total='277.43')
        line = Line.objects.create(document=doc, position=0, item_name='DR/727A-FW/TWBK', description='Chair', quantity=2, price='138.7125', tax_rate=0)
        line.refresh_from_db()
        self.assertEqual((line.price, line.amount), (Decimal('138.7125'), Decimal('277.43')))  # exact QB price, extended half-up
        save_document(dict(token=str(doc.token), version=doc.version, action='hold', customer_id=customer.pk,
                           lines=[dict(id=line.pk, quantity='2', price='138.71', tax_rate='0', item_name='DR/727A-FW/TWBK', description='Chair')]), 'sale', doc.token)
        line.refresh_from_db()
        self.assertEqual((line.recorded_amount, line.amount), (None, Decimal('277.42')))  # app rule once it's worked on
