# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Cashier
from customers.models import Customer
from purchasing.models import Department, PieceLabel, PurchaseDocument, TruckSession, Vendor
from reports import daily_totals, sales_tax, tax_periods
from sales.models import Document, GiftCertificate, GiftRedemption, Item, Line, OrderPayment
from sales.services import WorkflowError, save_document
from shop_settings.config import business, clear_cache, tax
from shop_settings.demo import CASHIERS


class DemoTests(TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)

    def load(self, **kwargs):
        with patch('shop_settings.demo.timezone.localdate', return_value=date(2026, 11, 4)):
            call_command('load_demo', stdout=StringIO(), verbosity=0, **kwargs)

    def test_catalog_settings_logo_and_no_identities(self):
        self.load()
        self.assertEqual((Department.objects.count(), Item.objects.count(), Vendor.objects.count(), Customer.objects.count()), (12, 250, 15, 120))
        self.assertEqual(Document.objects.count(), 0)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Cashier.objects.exists())
        self.assertEqual(business().name, 'Demo Home & Hardware')
        self.assertEqual(business().phone, '555-0100')
        self.assertTrue(business().logo.storage.exists(business().logo.name))
        self.assertEqual(tax().default_rate, Decimal('7'))
        self.assertEqual(tax().tax_year_start_month, 1)
        self.assertTrue(Item.objects.filter(on_hand=0).exists())
        self.assertFalse(Item.objects.filter(on_hand__lt=0).exists())
        self.assertEqual(Customer.objects.filter(charge_account=True).count(), 12)
        for customer in Customer.objects.filter(tax_exempt=True):
            self.assertTrue(customer.source_data['exemption_certificate'])
        self.assertTrue(Item.objects.exclude(price_2=None).exclude(price_3=None).exists())

    def test_refusal_append_and_reset_catalog(self):
        self.load()
        before = Item.objects.count()
        with self.assertRaisesMessage(CommandError, 'empty shop'):
            self.load()
        self.assertEqual(Item.objects.count(), before)
        self.load(force=True)
        self.assertEqual(Item.objects.count(), 500)
        self.load(reset_demo=True)
        self.assertEqual(Item.objects.count(), 250)
        Item.objects.create(source_id='DEMO-untagged', sku='REAL', description='Not demo', price=10, on_hand=0, tax_rate=7)
        with self.assertRaisesMessage(CommandError, 'without demo source IDs'):
            self.load(reset_demo=True, force=True)
        self.assertEqual(Item.objects.count(), 251)

    def test_invalid_days_are_atomic(self):
        with self.assertRaisesMessage(CommandError, 'Days must'):
            self.load(days=0)
        self.assertFalse(Item.objects.exists())

    def test_small_history_counts_workflows_and_ledger(self):
        self.load(with_history=True, days=10, seed=7)
        self.assertEqual(Item.objects.count(), 250)
        self.assertGreaterEqual(Document.objects.filter(kind='sale').count(), 125)
        self.assertEqual(Document.objects.filter(kind='order').count(), 5)
        self.assertEqual(PurchaseDocument.objects.filter(kind='po').count(), 5)
        self.assertEqual(PurchaseDocument.objects.filter(kind='voucher').count(), 3)
        self.assertTrue(TruckSession.objects.filter(status='received').exists())
        self.assertEqual(PieceLabel.objects.values('line__document').distinct().count(), 5)
        self.assertTrue(GiftCertificate.objects.exists())
        self.assertTrue(GiftRedemption.objects.exists())
        self.assertTrue(Line.objects.filter(quantity__lt=0, returned_from_line__isnull=False).exists())
        self.assertTrue(Document.objects.filter(kind='sale', status='cancelled').exists())
        self.assertTrue(Document.objects.filter(kind='order', status='cancelled', payments__amount__lt=0).exists())
        self.assertTrue(Document.objects.filter(kind='order', status='completed').exists())
        self.assertTrue(Document.objects.filter(status='held', hold_expired_at__isnull=False).exists())
        self.assertTrue(Document.objects.filter(status='held', hold_until=None).exists())
        self.assertTrue(Customer.objects.filter(charge_account=True, app_account_activity__gt=0).exists())
        self.assertFalse(Item.objects.filter(on_hand__lt=0).exists())
        self.assertEqual(set(Document.objects.values_list('cashier', flat=True)), set(CASHIERS))
        for doc in Document.objects.all():
            self.assertFalse(doc.imported)
            self.assertFalse(doc.from_quickbooks)
            day = timezone.localtime(doc.created_at)
            self.assertLess(day.weekday(), 5)
            self.assertTrue(10 <= day.hour < 18)
            self.assertTrue(date(2026, 10, 25) <= day.date() < date(2026, 11, 4))
        self.assertTrue(all(not po.imported for po in PurchaseDocument.objects.all()))
        for day in sorted(set(timezone.localdate(d) for d in Document.objects.values_list('created_at', flat=True))):
            period = tax_periods.resolve('custom', '', day, day, day)
            daily = daily_totals.build(period)
            sales = sales_tax.build(period)
            self.assertEqual(daily.totals['tax'], sales.tax, day)
        before = Document.objects.count()
        with self.assertRaisesMessage(CommandError, 'truck sessions have no source_id'):
            self.load(reset_demo=True, with_history=True, days=10)
        self.assertEqual(Document.objects.count(), before)
        # Working a tagged hold must not strip its demo ownership or treat it as
        # imported read-only history.
        held = Document.objects.filter(status='held').first()
        source = held.source_id
        cancelled = save_document(dict(action='cancel', version=held.version), 'sale', held.token, cashier=CASHIERS[0])
        self.assertEqual(cancelled.source_id, source)
        self.assertEqual(cancelled.status, 'cancelled')

    def test_preserve_settings_and_rollback_on_service_error(self):
        original = business()
        original.name = 'Example Test Workshop'
        original.save()
        self.load(preserve_settings=True)
        self.assertEqual(business().name, 'Example Test Workshop')
        with patch('sales.services.save_document', side_effect=WorkflowError('Example validation error')):
            with self.assertRaisesMessage(CommandError, 'Example validation error'):
                self.load(force=True, with_history=True, days=10)
        self.assertEqual(Item.objects.count(), 250)
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(business().name, 'Example Test Workshop')

    def test_same_seed_has_same_document_totals(self):
        snapshots = []
        for _ in range(2):
            with transaction.atomic():
                self.load(with_history=True, days=10, seed=31)
                snapshots.append((list(Document.objects.order_by('pk').values_list('kind', 'status', 'subtotal', 'discount', 'tax', 'total', 'payment_breakdown')),
                                  list(PurchaseDocument.objects.order_by('pk').values_list('kind', 'status', 'total'))))
                transaction.set_rollback(True)
            clear_cache()
        self.assertEqual(snapshots[0], snapshots[1])

    @override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware',
                                   'django.contrib.auth.middleware.AuthenticationMiddleware'])
    def test_wizard_keeps_small_fixture_and_can_select_history(self):
        from shop_settings import setup
        from accounts.roles import ADMIN_GROUP, group
        user = get_user_model().objects.create_user('demo-test-admin')
        user.groups.add(group(ADMIN_GROUP))
        self.client.force_login(user)
        self.assertContains(self.client.get('/setup/demo/'), 'With 4 months of sample history')
        with patch('shop_settings.setup.call_command') as command:
            response = self.client.post('/setup/demo/', {'with_history': 'on'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(command.call_args.args, ('load_demo',))
            self.assertTrue(command.call_args.kwargs['with_history'])
            self.assertTrue(command.call_args.kwargs['preserve_settings'])
        setup.load_demo()
        self.assertEqual(Item.objects.count(), 12)
