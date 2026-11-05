# SPDX-License-Identifier: AGPL-3.0-or-later
from uuid import UUID

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from .config import business, clear_cache
from .forms import BusinessForm
from .models import Business


class ExchangeIdentityTests(TestCase):
    def test_identity_survives_settings_edits_and_cache_reloads(self):
        row = business()
        identity = row.exchange_id
        self.assertIsInstance(identity, UUID)
        row.name = 'Demo Home & Hardware'
        row.save()
        clear_cache()
        self.assertEqual(business().exchange_id, identity)
        self.assertNotIn('exchange_id', BusinessForm().fields)
        self.assertNotEqual(Business.objects.create(name='Another Demo Shop').exchange_id, identity)


class ExchangeIdentityMigrationTests(TransactionTestCase):
    def test_existing_businesses_get_distinct_persisted_ids(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [('shop_settings', '0003_business')]
        try:
            executor.migrate(previous)
            OldBusiness = executor.loader.project_state(previous).apps.get_model('shop_settings', 'Business')
            ids = [OldBusiness.objects.create(name=name).pk for name in ('Demo Shop A', 'Demo Shop B')]
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            identities = list(Business.objects.filter(pk__in=ids).values_list('exchange_id', flat=True))
            self.assertEqual(len(set(identities)), 2)
            self.assertTrue(all(isinstance(value, UUID) for value in identities))
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(list(Business.objects.filter(pk__in=ids).values_list('exchange_id', flat=True)), identities)
        finally:
            MigrationExecutor(connection).migrate(latest)
            clear_cache()
