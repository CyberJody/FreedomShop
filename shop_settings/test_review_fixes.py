# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression cases for the Build 1 settings review. All fixtures are synthetic."""
from datetime import datetime
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.utils import timezone

from customers.models import Customer
from reports import tax_ledger
from sales.models import Document, Line
from shop_settings.config import clear_cache, tax
from shop_settings.models import TaxLocation
from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin


class HistoricalEstimateTests(TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)

    def test_rate_edit_does_not_rewrite_unexplained_payment_or_refund(self):
        customer = Customer.objects.create(full_name='Demo Buyer')
        when = timezone.make_aware(datetime(2026, 5, 4, 12))
        for amount in (D('107'), D('-214')):
            doc = Document.objects.create(kind='sale', status='completed', customer=customer,
                                          completed_at=when, total=amount, subtotal=amount,
                                          payment_breakdown=[{'method': 'cash', 'amount': str(amount)}])
            Line.objects.create(document=doc, source_id=str(amount), position=0, item_name=tax_ledger.POA_NAME, quantity=1 if amount > 0 else -1, price=abs(amount), tax_rate=0)
        before = [ev.tax for ev in tax_ledger.replay(tax_ledger.customer_events(customer.pk)).events]
        self.assertEqual(before, [D('7'), D('-14')])
        location = TaxLocation.objects.get(name=tax().default_location.name)
        location.rate = D('8.875')
        location.save()
        after = [ev.tax for ev in tax_ledger.replay(tax_ledger.customer_events(customer.pk)).events]
        self.assertEqual(after, before)


class AccountCorrectionTests(TestCase):
    def test_paid_corrections_and_exempt_transition_keep_each_charge_location(self):
        ledger = tax_ledger.Ledger()
        for key, place, amount, tax_amount in ((1, 'Harbor', '108.88', '8.88'), (2, 'Upland', '105', '5')):
            ledger.charge(tax_ledger.Lot(D(amount), D(tax_amount), D('100'), place, key))
        ledger.pay(D('213.88'), False)
        adjustments = ledger.adjust_charge({1, 2}, D('-13.88'), D('-200'), 'Exempt')
        self.assertEqual({lot.location: lot.tax for lot in adjustments}, {'Harbor': D('-8.88'), 'Upland': D('-5')})
        # A partial correction leaves tax on an unpaid Harbor lot: it must stay in Harbor.
        other = tax_ledger.Ledger()
        other.charge(tax_ledger.Lot(D('108.88'), D('8.88'), D('100'), 'Harbor', 3))
        other.adjust_charge({3}, D('-1'), D('1'), 'Exempt')
        self.assertEqual(other.lots[0].location, 'Harbor')

    def test_equal_paid_and_unpaid_halves_still_book_the_paid_correction(self):
        ledger = tax_ledger.Ledger()
        ledger.charge(tax_ledger.Lot(D('107'), D('7'), D('100'), 'Harbor', 1))
        ledger.pay(D('53.50'), False)
        corrections = ledger.adjust_charge({1}, D('-7'), D('-100'), 'Exempt')
        self.assertEqual([(lot.location, lot.tax, lot.taxable) for lot in corrections], [('Harbor', D('-3.50'), D('-50'))])
        self.assertEqual((ledger.lots[0].tax, ledger.lots[0].location), (D('0'), 'Exempt'))

    def test_paid_share_is_recognized_when_open_correction_makes_lots_equal(self):
        ledger = tax_ledger.Ledger()
        ledger.charge(tax_ledger.Lot(D('107'), D('7.01'), D('80'), 'Harbor', 1))
        ledger.pay(D('53.50'), False)
        corrections = ledger.adjust_charge({1}, D('.02'), D('0'), 'Harbor')
        self.assertEqual([(lot.location, lot.tax) for lot in corrections], [('Harbor', D('.01'))])


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class ReferenceGuardTests(TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)
        sign_in_admin(self.client)

    def test_both_payment_lock_tables_prevent_location_deletion(self):
        from sales.models import OrderPayment
        from shop_settings.views import location_in_use
        order = Document.objects.create(kind='order', status='open')
        for model in (Document, OrderPayment):
            with self.subTest(model=model.__name__):
                place = TaxLocation.objects.create(name='Harbor ' + model.__name__, rate=7)
                values = dict(locked_location=place.name.swapcase() + ' ', locked_tax=7)
                if model is Document:
                    row = model.objects.create(kind='sale', status='completed', **values)
                else:
                    row = model.objects.create(order=order, amount=107, method='cash', **values)
                self.assertTrue(location_in_use(place))
                response = self.client.post('/settings/tax/', {'action': 'location-delete', 'pk': place.pk})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(TaxLocation.objects.filter(pk=place.pk).exists())
                response = self.client.post('/settings/tax/', {'action': 'location-retire', 'pk': place.pk})
                self.assertEqual(response.status_code, 302)
                row.refresh_from_db()
                self.assertEqual(row.locked_tax, D('7'))

    def test_customer_document_department_and_item_references_allow_retirement_only(self):
        from purchasing.models import Department
        from sales.models import Item
        from shop_settings.models import TaxCategory
        for kind in ('customer', 'document'):
            place = TaxLocation.objects.create(name='Demo ' + kind, rate=7)
            if kind == 'customer':
                Customer.objects.create(full_name='Demo Buyer', tax_location=place.name.swapcase() + ' ')
            else:
                Document.objects.create(kind='order', status='open', tax_location=place.name.swapcase() + ' ')
            self.assertEqual(self.client.post('/settings/tax/', {'action': 'location-delete', 'pk': place.pk}).status_code, 400)
        for kind in ('department', 'item'):
            category = TaxCategory.objects.create(name='Demo ' + kind, taxable=True)
            if kind == 'department':
                Department.objects.create(name='Demo Department', tax_category=category)
            else:
                Item.objects.create(name='Demo Item', sku='DEMO', tax_category=category.name.swapcase() + ' ')
            self.assertEqual(self.client.post('/settings/tax/', {'action': 'category-delete', 'pk': category.pk}).status_code, 400)
            self.assertEqual(self.client.post('/settings/tax/', {'action': 'category-retire', 'pk': category.pk}).status_code, 302)


class CategoryRateTests(TestCase):
    def test_category_rate_supplies_blank_forms_without_overriding_explicit_rates(self):
        from config.tax import coherent_tax
        from purchasing.forms import DepartmentForm, ItemForm
        from purchasing.item_views import new_item_tax
        from purchasing.models import Department
        from shop_settings.models import TaxCategory
        reduced = TaxCategory.objects.create(name='Reduced', taxable=True, default_rate=4)
        self.addCleanup(clear_cache)
        self.assertEqual(coherent_tax('Reduced', None), ('Reduced', D('4')))
        self.assertEqual(coherent_tax('Reduced', D('6')), ('Reduced', D('6')))
        department = DepartmentForm({'name': 'Demo Department', 'tax_category': 'Reduced', 'tax_rate': '',
                                     'margin': '0', 'markup': '0', 'version': '1'})
        self.assertTrue(department.is_valid(), department.errors)
        self.assertEqual(department.cleaned_data['tax_rate'], D('4'))
        item = ItemForm({'name': 'Demo Item', 'item_type': 'inventory', 'price': '100', 'tax_category': 'Reduced', 'tax_rate': ''})
        item.is_valid()
        self.assertEqual(item.cleaned_data['tax_rate'], D('4'))
        default = TaxCategory.objects.get(name=tax().taxable_category.name)
        default.default_rate = D('3')
        default.save()
        self.assertEqual(new_item_tax()['tax_rate'], D('3'))
        self.assertEqual(Department(name='Demo').tax_rate, D('3'))


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class CategoryRoleTests(TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)
        sign_in_admin(self.client)

    def test_cannot_change_the_last_active_category_in_either_direction(self):
        from shop_settings.models import TaxCategory
        for taxable in (False, True):
            category = TaxCategory.objects.get(taxable=taxable)
            prefix = f'cat-{category.pk}'
            data = {'action': 'category', 'pk': category.pk, f'{prefix}-sort': '0'}
            if not taxable:
                data[f'{prefix}-taxable'] = 'on'
            response = self.client.post('/settings/tax/', data)
            self.assertEqual(response.status_code, 400)
            category.refresh_from_db()
            self.assertEqual(category.taxable, taxable)
            self.assertTrue(TaxCategory.objects.filter(active=True, taxable=taxable).exists())


class LocationAliasTests(TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)

    def test_migration_and_readers_share_normalized_location_identity(self):
        from importlib import import_module
        from unittest.mock import Mock
        from django.apps import apps
        from django.db import connection
        from shop_settings.models import TaxSettings
        buyers = [Customer.objects.create(source_id=name, full_name='Demo Buyer', tax_location=name)
                  for name in ('Harbor ', 'harbor', 'Exempt ', 'exempt')]
        TaxSettings.objects.all().delete()
        TaxLocation.objects.all().delete()
        import_module('shop_settings.migrations.0002_locations_from_documents').forwards(apps, Mock(connection=connection))
        config = tax()
        self.assertEqual(TaxLocation.objects.count(), 2)
        self.assertEqual(config.section_of(' HARBOR ', False).casefold(), 'harbor')
        for customer in buyers[2:]:
            self.assertTrue(tax_ledger.customer_exempt(customer, False))
            customer.refresh_from_db()
            self.assertIn(customer.tax_location, ('Exempt ', 'exempt'))
        TaxLocation.objects.create(name='Tax Free', is_exempt=True)
        self.assertTrue(tax_ledger.customer_exempt(Customer(tax_location=' TAX FREE '), False))
        self.assertEqual(config.section_of('exempt', False).casefold(), 'exempt')


class LongLocationTests(TestCase):
    def test_long_location_fits_both_payment_lock_columns(self):
        import uuid
        from sales.models import OrderPayment
        from sales.services import save_order_payment
        name = 'Harbor ' + 'a' * 93
        place = TaxLocation.objects.create(name=name, rate=7)
        place.full_clean()
        for model in (Document, OrderPayment):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.get_field('locked_location').clean(name, None), name)
        order = Document.objects.create(kind='order', status='open', tax_location=name, subtotal=100, tax=7, total=107)
        Line.objects.create(document=order, position=0, item_name='Demo Chair', quantity=1, price=100, tax_rate=7)
        save_order_payment(order.pk, {'version': order.version, 'token': str(uuid.uuid4()), 'amount': '107', 'method': 'cash'})
        payment = order.payments.get()
        self.assertEqual(payment.locked_location, name)
        self.assertEqual(payment.locked_tax, D('7'))


class ActiveCategoryDefaultTests(TestCase):
    def test_retired_categories_are_only_offered_on_existing_records(self):
        from purchasing.forms import DepartmentForm
        from purchasing.item_views import new_item_tax
        from purchasing.models import Department
        from shop_settings.models import TaxCategory
        self.addCleanup(clear_cache)
        retired = TaxCategory.objects.get(name=tax().taxable_category.name)
        current = TaxCategory.objects.create(name='Current Taxable', taxable=True, default_rate=4)
        existing = Department.objects.create(name='Demo Existing', tax_category=retired)
        retired.active = False
        retired.save()
        self.assertEqual(tax().taxable_category.name, current.name)
        self.assertEqual(Department(name='Demo New').tax_category_id, current.pk)
        self.assertEqual(new_item_tax(), {'tax_category': current.name, 'tax_rate': D('4')})
        self.assertNotIn(retired, DepartmentForm().fields['tax_category'].queryset)
        self.assertNotIn(retired, DepartmentForm(instance=Department(tax_category=retired)).fields['tax_category'].queryset)
        self.assertIn(retired, DepartmentForm(instance=existing).fields['tax_category'].queryset)
        old_non = TaxCategory.objects.get(name=tax().nontaxable_category.name)
        new_non = TaxCategory.objects.create(name='Current Non-taxable', taxable=False)
        old_non.active = False
        old_non.save()
        self.assertEqual(tax().nontaxable_category.name, new_non.name)
