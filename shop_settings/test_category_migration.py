# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department category conversion preserves every distinct name and its exact original text."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from shop_settings.config import clear_cache


class DepartmentCategoryMigrationTests(TransactionTestCase):
    def test_distinct_names_and_blank_strings_survive_forward_and_reverse(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [('purchasing', '0018_truck_receiving')]
        after = [('purchasing', '0019_department_tax_category_fk')]
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            Department = old.get_model('purchasing', 'Department')
            raw = ('Non', 'nOn', ' NON ', '', '   ', 'Prepared Food')
            ids = [Department.objects.create(name=f'Demo Department {n}', tax_category=name, tax_rate=0 if n < 5 else 4).pk
                   for n, name in enumerate(raw)]
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            converted = executor.loader.project_state(after).apps.get_model('purchasing', 'Department')
            linked = [converted.objects.select_related('tax_category').get(pk=pk) for pk in ids]
            self.assertEqual([d.tax_category.name for d in linked[:3]], list(raw[:3]))
            self.assertEqual(len({d.tax_category_id for d in linked[:3]}), 3)
            self.assertEqual([d.legacy_tax_category for d in linked], list(raw))
            executor = MigrationExecutor(connection)
            executor.migrate(before)
            restored = executor.loader.project_state(before).apps.get_model('purchasing', 'Department')
            self.assertEqual([restored.objects.get(pk=pk).tax_category for pk in ids], list(raw))
        finally:
            MigrationExecutor(connection).migrate(latest)
            clear_cache()
