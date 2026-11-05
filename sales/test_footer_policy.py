# SPDX-License-Identifier: AGPL-3.0-or-later
from django.test import SimpleTestCase
from sales.printing import FOOTER


class ReceiptFooterTests(SimpleTestCase):
    def test_only_approved_fee_percentage_changes(self):
        self.assertEqual(FOOTER, (
            'Cancellations accepted on order for stock items within 7 days for either Store Credit or a Refund '
            'less a 20% re-processing fee ($10 minimum). No Cancellations or Refunds on Custom Built, Custom '
            'Finished, Special Order, or Sale items. Unless prior arrangements are made, orders must be picked '
            'up within 14 days of notification or they will be sold and your deposit forfeited. $5/day storage '
            'charges after 60 days. All claims must be processed by this invoice.'))
