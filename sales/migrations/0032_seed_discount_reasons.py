# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fork migration: the original private-store data step is intentionally omitted.

Existing databases remain the store owner's responsibility; this migration does not
remove or repair their data. New installations configure their own users and printers.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('sales', '0031_discountreason_document_discount_note_and_more')]
    operations = []
