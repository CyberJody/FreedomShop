# SPDX-License-Identifier: AGPL-3.0-or-later
"""a PO line's Due (Ordered - Received, never below 0) is redrawn while Ordered is typed, and new lines show it."""
import uuid
from decimal import Decimal as D

from django.test import TestCase

from sales.models import Item
from .models import Vendor
from .services import save_purchase


class LiveDueTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='R68', company='R68 Vendor')
        self.item = Item.objects.create(sku='R68-1', name='R68 CHAIR', on_hand=0, order_cost=D('10'))

    def po(self, ordered, received):
        header = lambda: dict(token=uuid.uuid4(), version=0, vendor=self.vendor, status='open', notes='', terms='',
                              ship_date=None, payment_due=None, invoice_number='')
        po = save_purchase(header(), [dict(item=self.item, quantity=D(ordered), cost=D('10'))], 'po', 'save')
        if received:
            save_purchase(header(), [dict(item=self.item, quantity=D(received), cost=D('10'), source_line=po.lines.get().pk)],
                          'voucher', 'post', po)
        return po

    def test_saved_line_carries_received_for_the_live_due(self):
        page = self.client.get(f'/purchasing/po/{self.po(5, 2).token}/').content.decode()
        self.assertIn('<td class="received" data-received="2">2</td><td class="due">3</td>', page)

    def test_new_line_template_shows_due_with_nothing_received(self):
        page = self.client.get('/purchasing/po/new/').content.decode()
        template = page.split('<template id="empty-line">', 1)[1].split('</template>', 1)[0]
        self.assertIn('<td class="received" data-received="0">0</td><td class="due">', template)

    def test_script_redraws_due_from_ordered_minus_received(self):
        page = self.client.get('/purchasing/po/new/').content.decode()
        self.assertIn("const due=row.querySelector('.due')", page)
        self.assertIn("due.textContent=q===null?'—':String(q>got?q-got:0n)", page)   # never below 0
