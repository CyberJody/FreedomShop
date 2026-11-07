# SPDX-License-Identifier: AGPL-3.0-or-later
"""Do not evict an unexpired security counter when the cache reaches capacity."""
from django.core.cache.backends.db import DatabaseCache
from django.db import connections


class ReauthenticationCache(DatabaseCache):
    def _cull(self, db, cursor, now, num):
        connection = connections[db]
        quote = connection.ops.quote_name
        cursor.execute(f'DELETE FROM {quote(self._table)} WHERE {quote("expires")} < %s',
                       [connection.ops.adapt_datetimefield_value(now)])
