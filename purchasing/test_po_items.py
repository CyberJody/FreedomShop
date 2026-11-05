# SPDX-License-Identifier: AGPL-3.0-or-later
"""purchase-order items: editable PO line descriptions, Preferred / Alternate Pickup Date,
PO Direct Print defaults to Tray 2 of the office Brother."""
import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from sales.models import Item
from .models import PurchaseDocument, Vendor
from .services import save_purchase


def header(vendor, **kw):
    data = dict(token=uuid.uuid4(), version=0, vendor=vendor, status='open', notes='', terms='', ship_date=None,
                payment_due=None, invoice_number='')
    data.update(kw)
    return data


def post_data(vendor, item, description=None, **extra):
    data = {'token': str(uuid.uuid4()), 'version': '0', 'vendor': vendor.pk, 'status': 'open', 'lines-TOTAL_FORMS': '1',
            'lines-INITIAL_FORMS': '0', 'lines-0-item': item.pk, 'lines-0-quantity': '2', 'lines-0-cost': '20', 'action': 'save'}
    if description is not None:
        data['lines-0-item_description'] = description
    data.update(extra)
    return data


class POItemsBase(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(code='AMISH', company='Sample Builder')
        self.item = Item.objects.create(sku='SO-1', name='Special order table', description='Oak table 42x60',
                                        price=900, on_hand=0, average_cost=0, order_cost=400, primary_vendor=self.vendor, tax_rate=7)


class R49EditableDescriptionTests(POItemsBase):
    EDITED = 'Oak table 42x60\nOCS stain, 2 leaves, NO breadboard ends'

    def test_po_editor_saves_edited_text_on_the_line_only(self):
        response = self.client.post('/purchasing/po/new/', post_data(self.vendor, self.item, self.EDITED))
        self.assertEqual(response.status_code, 302)
        po = PurchaseDocument.objects.get(kind='po')
        self.assertEqual(po.lines.get().description, self.EDITED)
        self.item.refresh_from_db()
        self.assertEqual(self.item.description, 'Oak table 42x60')   # Item List unchanged
        page = self.client.get(response.url).content.decode()
        self.assertIn('OCS stain, 2 leaves', page)
        textarea = page[page.index('name="lines-0-item_description"') - 200:page.index('name="lines-0-item_description"') + 200]
        self.assertNotIn('readonly', textarea)                          # editable on the PO

    def test_blank_text_falls_back_to_the_item_and_long_text_is_refused(self):
        self.client.post('/purchasing/po/new/', post_data(self.vendor, self.item, '   '))
        self.assertEqual(PurchaseDocument.objects.get().lines.get().description, 'Oak table 42x60')
        self.assertEqual(self.client.post('/purchasing/po/new/', post_data(self.vendor, self.item, 'x' * 1001)).status_code, 400)

    def test_edit_prints_on_the_po_and_carries_to_the_voucher(self):
        from .print_exports import build, pdf_bytes
        po = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('2'), cost=Decimal('400'),
                                                      item_description=self.EDITED)], 'po', 'save')
        self.assertEqual(build(po)['rows'][0]['description'], self.EDITED)
        self.assertTrue(pdf_bytes(po).startswith(b'%PDF'))
        line = po.lines.get()
        receive = self.client.get(f'/purchasing/voucher/new/?po={po.token}').content.decode()
        self.assertIn('NO breadboard ends', receive)
        box = receive[receive.index('name="lines-0-item_description"') - 200:receive.index('name="lines-0-item_description"') + 200]
        self.assertIn('readonly', box)                                  # voucher box stays read-only
        # Even if a voucher posts different text, it keeps the PO line's text.
        voucher = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('400'),
                                                           source_line=line.pk, item_description='tampered')], 'voucher', 'post', po)
        self.assertEqual(voucher.lines.get().description, self.EDITED)

    def test_voucher_without_po_ignores_posted_text(self):
        voucher = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('400'),
                                                           item_description='typed')], 'voucher', 'post')
        self.assertEqual(voucher.lines.get().description, 'Oak table 42x60')

    def test_resave_keeps_the_edit_and_repicking_resets_in_the_script(self):
        po = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('2'), cost=Decimal('400'),
                                                      item_description=self.EDITED)], 'po', 'save')
        page = self.client.get(f'/purchasing/po/{po.token}/').content.decode()
        self.assertIn('NO breadboard ends', page)                     # editor reloads the line's own text
        # Picking an item (search result / Item List) puts the item's description back into the box.
        self.assertIn(".purchase-description').value=item.description", page)


class R50PickupDateTests(POItemsBase):
    def test_editor_labels_and_saves_both_dates(self):
        response = self.client.post('/purchasing/po/new/', post_data(self.vendor, self.item, ship_date='2026-11-03',
                                                                     alternate_pickup_date='2026-11-10'))
        self.assertEqual(response.status_code, 302)
        po = PurchaseDocument.objects.get()
        self.assertEqual((po.ship_date, po.alternate_pickup_date), (date(2026, 11, 3), date(2026, 11, 10)))
        page = self.client.get(response.url).content.decode()
        self.assertIn('Preferred Pickup Date', page)
        self.assertIn('Alternate Pickup Date', page)
        self.assertNotIn('Ship Date', page)
        self.assertIn('value="2026-11-10"', page)
        voucher_page = self.client.get(f'/purchasing/voucher/new/?po={po.token}').content.decode()
        self.assertNotIn('alternate_pickup_date', voucher_page)       # vouchers don't get the field

    def test_printout_box_emphasizes_preferred_and_keeps_blank_alternate_slot(self):
        from .print_exports import ALTERNATE, PREFERRED, build, pdf_bytes
        po = save_purchase(header(self.vendor, ship_date=date(2026, 11, 3)), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('400'))], 'po', 'save')
        labels = [label for label, _ in build(po)['fields']]
        self.assertNotIn('Ship Date', labels)
        self.assertEqual(labels.index(ALTERNATE), labels.index(PREFERRED) + 1)    # right below it
        self.assertIn((PREFERRED, '11/3/2026'), build(po)['fields'])
        self.assertIn((ALTERNATE, ''), build(po)['fields'])                       # blank prints as an empty slot
        po.alternate_pickup_date = date(2026, 11, 10)
        po.save()
        self.assertIn((ALTERNATE, '11/10/2026'), build(po)['fields'])
        self.assertTrue(pdf_bytes(po).startswith(b'%PDF'))

    def test_imported_po_shows_its_qb_ship_date_as_preferred(self):
        po = save_purchase(header(self.vendor, ship_date=date(2025, 5, 1)), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('400'))], 'po', 'save')
        PurchaseDocument.objects.filter(pk=po.pk).update(source_id='QB-1', source_number='8190')
        page = self.client.get(f'/purchasing/po/{po.token}/').content.decode()
        self.assertIn('Preferred Pickup Date', page)
        self.assertIn('5/1/2025', page)


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class R51POTrayTwoTests(POItemsBase):
    def setUp(self):
        super().setUp()
        from sales.models import Printer
        self.front = Printer.objects.create(name='Brother Front Counter', kind='letter', host='192.0.2.20', port=631)
        self.office = Printer.objects.create(name='Brother MFC Office', kind='letter', host='192.0.2.113', port=631,
                                             color=True, media_source='tray-1')
        self.po = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('400'))], 'po', 'save')

    def add_tray_two(self, host='192.0.2.113'):
        from sales.models import Printer
        return Printer.objects.create(name='Brother MFC Office – Tray 2', kind='letter', host=host, port=631, media_source='tray-2')

    def selected(self, path=None):
        return self.client.get(path or f'/purchasing/po/{self.po.token}/').context['selected_printer']

    def test_without_a_tray_two_row_po_keeps_the_old_default(self):
        self.assertEqual(self.selected(), str(self.front.pk))       # first Letter printer, as before

    def test_po_defaults_to_tray_two_and_remembers_its_own_choice(self):
        tray = self.add_tray_two()
        self.assertEqual(self.selected(), str(tray.pk))
        with mock.patch('reports.direct_print.print_report', return_value='Sent 1 page') as sent:
            response = self.client.post(f'/purchasing/po/{self.po.token}/print/', {'printer': self.front.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent.call_args.args[0], self.front)
        self.assertEqual(self.selected(), str(self.front.pk))       # PO remembers its last printer
        self.assertNotIn('report_printer', self.client.session)      #... without touching the reports' choice
        with mock.patch('reports.direct_print.print_report', return_value='Sent 1 page') as sent:
            self.client.post(f'/purchasing/po/{self.po.token}/print/', {'printer': tray.pk})
        self.assertEqual(sent.call_args.args[0].media_source, 'tray-2')
        self.assertEqual(self.client.session['po_printer'], str(tray.pk))

    def test_vouchers_reports_and_set_tags_are_not_moved_to_tray_two(self):
        tray = self.add_tray_two()
        voucher = save_purchase(header(self.vendor), [dict(item=self.item, quantity=Decimal('1'), cost=Decimal('400'))], 'voucher', 'post')
        self.assertNotEqual(self.selected(f'/purchasing/voucher/{voucher.token}/'), str(tray.pk))
        from reports.direct_print import letter_printers
        from tags.views import set_tag_printer
        request = mock.Mock(session={})
        self.assertEqual(set_tag_printer(request, letter_printers()), str(self.office.pk))   # set tags stay on Tray 1

    def test_tray_two_on_the_office_host_wins_over_another_tray_two(self):
        self.add_tray_two(host='192.0.2.99')   # a Tray 2 on some other printer
        from sales.models import Printer
        mine = Printer.objects.create(name='Office Tray 2', kind='letter', host='192.0.2.113', port=631, media_source='tray-2')
        self.assertEqual(self.selected(), str(mine.pk))
