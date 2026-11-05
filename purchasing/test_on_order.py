# SPDX-License-Identifier: AGPL-3.0-or-later
"""(item on-order visibility) and (on order counts only POs dated 2026 or later)."""
import json
import uuid
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from sales.models import Item
from .models import Vendor, PurchaseDocument, PurchaseLine


def store_time(y, m, d, hour=12):
    return timezone.make_aware(datetime(y, m, d, hour), timezone.get_default_timezone())


class OnOrderFixture(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='HOO', company='EXAMPLE FURNITURE')
        self.item = Item.objects.create(sku='6297', name='HOO-CHAIR-OAK', description='SIDE CHAIR', price=199,
                                        on_hand=2, average_cost=90, order_cost=90, primary_vendor=self.vendor, tax_rate=7,
                                        reorder_point=5)

    def po(self, when, quantity, received=0, imported=False, ship=None, item=None, number=''):
        doc = PurchaseDocument.objects.create(kind='po', status='open', vendor=self.vendor, vendor_name=self.vendor.company,
                                              ship_date=ship, source_id=f'QB-{uuid.uuid4()}' if imported else None,
                                              source_number=number)
        PurchaseDocument.objects.filter(pk=doc.pk).update(created_at=when)
        doc.refresh_from_db()
        PurchaseLine.objects.create(document=doc, position=1, item=item or self.item, sku='6297', item_name='HOO-CHAIR-OAK',
                                    description='SIDE CHAIR', quantity=Decimal(quantity), cost=Decimal('90'),
                                    received=Decimal(received), imported_received=Decimal(received) if imported else 0,
                                    source_id=f'L-{uuid.uuid4()}' if imported else None)
        return doc


class R106VisibilityTests(OnOrderFixture):
    def setUp(self):
        super().setUp()
        self.soon = self.po(store_time(2026, 9, 1), 5, ship=date(2026, 11, 2), number='8101')
        self.later = self.po(store_time(2026, 10, 1), 5, number='8150')

    def test_details_panel_lists_open_pos_inline_with_expected(self):
        page = self.client.get(f'/purchasing/items/{self.item.pk}/details/').content.decode()
        self.assertIn('<dt>On Order</dt><dd>10</dd>', page)
        start = page.index('data-open-pos')
        table = page[start:page.index('</table>', start)]
        self.assertIn('<th>Expected</th>', table)
        self.assertIn('PO-8101', table); self.assertIn('PO-8150', table)
        self.assertLess(table.index('PO-8101'), table.index('PO-8150'))     # oldest first
        self.assertIn('<td>11/02/2026</td>', table)                          # Preferred Pickup Date
        self.assertIn('<td>—</td><td class="number">5</td>', table)          # blank date -> dash
        self.assertIn(f'/purchasing/po/{self.soon.token}/', table)
        self.assertIn('EXAMPLE FURNITURE', table)

    def test_orders_popup_po_rows_have_expected(self):
        page = self.client.get(f'/purchasing/items/{self.item.pk}/details/').content.decode()
        popup = page[page.index('data-popup="orders"'):]
        self.assertIn('<th>Expected</th>', popup)
        self.assertIn('<td>EXAMPLE FURNITURE</td><td>11/02/2026</td><td class="number">5</td>', popup)
        self.assertIn('data-detail-popup="orders"', page)                    # the Orders button stays

    def test_item_without_pos_says_so(self):
        other = Item.objects.create(sku='1', name='NONE', description='X', price=1, on_hand=1, tax_rate=7)
        page = self.client.get(f'/purchasing/items/{other.pk}/details/').content.decode()
        self.assertIn('<dt>On Order</dt><dd>0</dd>', page)
        self.assertIn('No open purchase orders for this item.', page)

    def test_item_list_on_order_cell_hook_only_when_above_zero(self):
        other = Item.objects.create(sku='2', name='AAA-NOTHING', description='X', price=1, on_hand=1, tax_rate=7)
        page = self.client.get('/purchasing/items/?view=pages').content.decode()
        self.assertIn(f'class="on-order-link" data-item-orders="{self.item.pk}"', page)
        self.assertNotIn(f'data-item-orders="{other.pk}"', page)
        rows = json.loads(self.client.get('/purchasing/items/?format=rows&offset=0').content)['rows']
        flags = {row['id']: row['orders'] for row in rows}
        self.assertEqual((flags[self.item.pk], flags[other.pk]), (True, False))

    def test_item_list_sorts_by_on_order(self):
        Item.objects.create(sku='2', name='AAA-NOTHING', description='X', price=1, on_hand=1, tax_rate=7)
        page = self.client.get('/purchasing/items/?view=pages&sort=-on_order')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['page'][0].pk, self.item.pk)

    def test_edit_item_lists_open_pos(self):
        page = self.client.get(f'/purchasing/items/{self.item.pk}/').content.decode()
        self.assertIn('<details class="item-panel" open><summary>Available / On Order</summary>', page)
        self.assertIn('data-open-pos', page)
        self.assertIn('PO-8101', page); self.assertIn('<td>11/02/2026</td>', page)


class R107CutoffTests(OnOrderFixture):
    """On order = open PO lines linked to the item on POs dated on/after 1/1/2026 (store time), imported or native."""

    def test_cutoff_is_new_year_store_time(self):
        from .catalog_quantities import ON_ORDER_FROM, on_order_for
        self.assertEqual(ON_ORDER_FROM, date(2026, 1, 1))
        self.po(store_time(2025, 12, 31, 23), 4, imported=True, number='7001')   # 11 PM 12/31/2025 in Springfield
        self.assertEqual(on_order_for(self.item), 0)
        self.po(store_time(2026, 1, 1, 0), 3, imported=True, number='7002')      # midnight 1/1/2026 counts
        self.assertEqual(on_order_for(self.item), 3)

    def test_imported_received_honoured_and_native_counts(self):
        from .catalog_quantities import on_order_for
        self.po(store_time(2026, 3, 5), 10, received=6, imported=True, number='7100')   # QB received 6 of 10
        self.po(store_time(2026, 10, 20), 2)                                              # app-made PO
        self.po(store_time(2024, 6, 1), 9, imported=True, number='6000')                  # old QB PO: not counted
        self.assertEqual(on_order_for(self.item), 6)

    def test_qb_figure_on_item_is_not_read(self):
        from .catalog_quantities import catalog_quantities
        Item.objects.filter(pk=self.item.pk).update(imported_on_order=30)
        self.po(store_time(2026, 2, 1), 12, imported=True, number='7200')
        self.assertEqual(catalog_quantities(Item.objects.filter(pk=self.item.pk)).get().on_order, 12)

    def test_closed_cancelled_and_unlinked_lines_never_count(self):
        from .catalog_quantities import on_order_for
        closed = self.po(store_time(2026, 2, 1), 5)
        PurchaseDocument.objects.filter(pk=closed.pk).update(status='closed')
        cancelled = self.po(store_time(2026, 2, 1), 5)
        PurchaseDocument.objects.filter(pk=cancelled.pk).update(status='cancelled')
        loose = self.po(store_time(2026, 2, 1), 5, imported=True)
        loose.lines.update(item=None)
        self.assertEqual(on_order_for(self.item), 0)

    def test_every_screen_reads_the_same_figure(self):
        from . import reorder
        from .catalog_quantities import catalog_quantities
        Item.objects.filter(pk=self.item.pk).update(imported_on_order=30)   # QB's old total (from an old PO)
        self.po(store_time(2019, 4, 2), 18, imported=True, number='5124')
        self.po(store_time(2026, 8, 3), 12, imported=True, number='8001', ship=date(2026, 12, 1))
        self.assertEqual(catalog_quantities(Item.objects.filter(pk=self.item.pk)).get().on_order, 12)
        listing = self.client.get('/purchasing/items/?view=pages')
        self.assertEqual(listing.context['page'][0].on_order, 12)
        rows = json.loads(self.client.get('/purchasing/items/?format=rows&offset=0').content)['rows']
        self.assertEqual(rows[0]['cells'][5], '12')
        picker = json.loads(self.client.get('/purchasing/item-picker/?q=HOO').content)['results'][0]
        self.assertEqual(picker['on_order'], '12')
        detail = self.client.get(f'/purchasing/items/{self.item.pk}/details/')
        self.assertEqual(detail.context['on_order'], 12)
        self.assertFalse(detail.context['po_mismatch'])                       # recomputed from lines: no QB note
        self.assertNotIn('QuickBooks import difference', detail.content.decode())
        edit = self.client.get(f'/purchasing/items/{self.item.pk}/')
        self.assertEqual(edit.context['on_order'], 12)
        row = next(r for r in reorder.evaluate() if r['item'] == self.item)
        self.assertEqual((row['on_order'], row['projected']), (12, 14))       # 2 on hand + 12

    def test_reorder_reminder_appears_when_only_old_pos_cover_it(self):
        from . import reorder
        self.po(store_time(2023, 5, 1), 10, imported=True, number='6500')
        self.assertIn(self.item, [r['item'] for r in reorder.reminders()[0]])   # 2 on hand <= 5; the 2023 PO no longer covers it

    def test_old_pos_listed_greyed_after_counted_rows(self):
        self.po(store_time(2019, 4, 2), 18, imported=True, number='5124')
        self.po(store_time(2026, 8, 3), 12, imported=True, number='8001')
        page = self.client.get(f'/purchasing/items/{self.item.pk}/details/').content.decode()
        self.assertIn('<dt>On Order</dt><dd>12</dd>', page)
        start = page.index('data-open-pos')
        table = page[start:page.index('</table>', start)]
        self.assertLess(table.index('PO-8001'), table.index('PO-5124'))     # counted rows first
        self.assertEqual(table.count('class="po-not-counted"'), 1)
        self.assertIn('04/02/2019 <span class="po-not-counted-note">before 2026 — not counted</span>', table)
        popup = page[page.index('data-popup="orders"'):]
        self.assertIn('class="po-not-counted"', popup)
        self.assertIn('Purchase orders dated before 2026 are greyed and not counted in On Order.', popup)
        edit = self.client.get(f'/purchasing/items/{self.item.pk}/').content.decode()
        self.assertIn('before 2026 — not counted', edit)

    def test_only_old_pos_means_no_item_list_link(self):
        self.po(store_time(2019, 4, 2), 18, imported=True, number='5124')
        page = self.client.get('/purchasing/items/?view=pages').content.decode()
        self.assertNotIn(f'data-item-orders="{self.item.pk}"', page)
        rows = json.loads(self.client.get('/purchasing/items/?format=rows&offset=0').content)['rows']
        self.assertEqual((rows[0]['cells'][5], rows[0]['orders']), ('0', False))
