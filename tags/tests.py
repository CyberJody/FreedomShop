# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tag Generator: Furniture Set Tag, checked against a shop's QB set sign photo (Eagle Ridge set)."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from sales.models import Item, Printer
from .models import DEFAULT_TAGLINE, FurnitureSet, FurnitureSetLine
from .sets import build, pdf_bytes


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class FurnitureSetTagTests(TestCase):
    def setUp(self):
        self.table = Item.objects.create(sku='16243', name='TMW-EGL4272-2/12-RWO-ALMND', price='1899.950000', tax_rate='7.000',
                                         description='"EAGLE RIDGE" TRESTLE TABLE\r\n42 X 72 WITH 2 - 12" LEAVES\r\nRUSTIC WHITE OAK FINISHED FC42000 ALMOND')
        self.chair = Item.objects.create(sku='16244', name='TMW-EGLSIDE-RWO-ALMND', price='464.950000', tax_rate='7.000',
                                         description='EAGLE SIDE CHAIR\r\nRUSTIC WHITE OAK FINISHED FC42000 ALMOND')

    def save(self, token=None, **extra):
        data = dict(name='Eagle Ridge dining set', tagline=DEFAULT_TAGLINE, copies='2', item=[self.table.pk, self.chair.pk], quantity=['1', '8'], then='stay')
        data.update(extra)
        return self.client.post(f'/tags/furniture-sets/{token}/' if token else '/tags/furniture-sets/new/', data)

    def test_eagle_ridge_set_matches_the_photo_with_half_up_tax(self):
        response = self.save()
        tag_set = FurnitureSet.objects.get()
        self.assertRedirects(response, f'/tags/furniture-sets/{tag_set.token}/?saved=1', fetch_redirect_response=False)
        data = build(tag_set)
        self.assertEqual([(r['item_name'], r['price'], r['quantity'], r['ext']) for r in data['rows']],
                         [('TMW-EGL4272-2/12-RWO-ALMND', Decimal('1899.95'), Decimal('1'), Decimal('1899.95')),
                          ('TMW-EGLSIDE-RWO-ALMND', Decimal('464.95'), Decimal('8'), Decimal('3719.60'))])
        # Subtotal $5,619.55 at 7% is $393.3685, rounded half-up to $393.37.
        self.assertEqual((data['subtotal'], data['tax'], data['total'], data['pieces']), (Decimal('5619.55'), Decimal('393.37'), Decimal('6012.92'), Decimal('9')))
        self.assertEqual(data['tax_label'], 'Springfield   7 % Tax:')
        self.assertEqual(data['tagline'], ['THIS SET AVAILABLE NOW', 'RESERVE YOURS TODAY!'])

    def test_prices_come_from_the_item_list_at_print_time(self):
        self.save()
        Item.objects.filter(pk=self.chair.pk).update(price='499.999')
        data = build(FurnitureSet.objects.get())
        self.assertEqual(data['rows'][1]['price'], Decimal('500.00'))
        self.assertEqual(data['subtotal'], Decimal('5899.95'))

    def test_deleted_item_keeps_its_name_and_untaxed_items(self):
        self.save()
        tag_set = FurnitureSet.objects.get()
        Item.objects.filter(pk=self.table.pk).update(tax_rate='0')
        self.chair.delete()
        data = build(tag_set)
        self.assertTrue(data['rows'][1]['missing'])
        self.assertEqual(data['rows'][1]['item_name'], 'TMW-EGLSIDE-RWO-ALMND')
        self.assertEqual((data['tax'], data['tax_label']), (Decimal('0.00'), 'Tax:'))
        self.assertContains(self.client.get(f'/tags/furniture-sets/{tag_set.token}/'), 'No longer in the Item List')

    def test_edit_replaces_lines_and_validates(self):
        self.save()
        tag_set = FurnitureSet.objects.get()
        self.save(tag_set.token, item=[self.chair.pk], quantity=['4'], name='Eagle chairs', tagline='')
        tag_set.refresh_from_db()
        self.assertEqual((tag_set.name, tag_set.tagline, [(l.item_id, l.quantity) for l in tag_set.lines.all()]), ('Eagle chairs', '', [(self.chair.pk, Decimal('4'))]))
        for bad, message in [(dict(item=[], quantity=[]), 'Add at least one item'), (dict(quantity=['0', '8']), 'must be a whole number (1 to 9999)'),
                             (dict(name=''), 'Enter a name'), (dict(item=['999999', self.chair.pk]), 'no longer in the Item List'),
                             (dict(tagline='1\n2\n3\n4\n5'), 'up to 4 lines'), (dict(quantity=['1.0001', '8']), 'must be a whole number (1 to 9999)'),
                             (dict(quantity=['2.5', '8']), 'must be a whole number (1 to 9999)')]:  # no fractional set quantities
            response = self.save(tag_set.token, **bad)
            self.assertContains(response, message, status_code=400)
        self.assertEqual(FurnitureSetLine.objects.count(), 1)

    def test_item_list_selection_prefills_a_new_set(self):
        page = self.client.get(f'/tags/furniture-sets/new/?item={self.table.pk}&item={self.chair.pk}&item=bad')
        self.assertContains(page, 'TMW-EGL4272-2/12-RWO-ALMND')
        self.assertContains(page, 'TMW-EGLSIDE-RWO-ALMND')
        self.assertContains(page, 'THIS SET AVAILABLE NOW')
        self.assertContains(self.client.get('/purchasing/items/'), 'data-selected-target="/tags/furniture-sets/new/"')

    def test_pdf_copies_index_and_delete(self):
        self.save()
        tag_set = FurnitureSet.objects.get()
        response = self.client.get(f'/tags/furniture-sets/{tag_set.token}/pdf/?copies=2')
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('Furniture-Set-Tag-Eagle-Ridge-dining-set.pdf', response['Content-Disposition'])
        from pypdfium2 import PdfDocument
        self.assertEqual(len(PdfDocument(response.content)), 2)
        self.assertEqual(len(PdfDocument(pdf_bytes(tag_set))), 1)
        index = self.client.get('/tags/')
        self.assertContains(index, 'Furniture Set Tag')
        self.assertContains(index, 'Eagle Ridge dining set')
        self.assertContains(index, '$6012.92')
        self.assertContains(self.client.get('/'), 'href="/tags/"')
        response = self.client.post(f'/tags/furniture-sets/{tag_set.token}/delete/')
        self.assertIn('deleted=Eagle', response['Location'])
        self.assertFalse(FurnitureSet.objects.exists())
        self.assertTrue(Item.objects.filter(pk=self.chair.pk).exists())

    def test_direct_print_sends_copies_to_a_letter_printer(self):
        self.save()
        tag_set = FurnitureSet.objects.get()
        brother = Printer.objects.create(name='West Counter Brother', kind='letter', host='192.0.2.10', active=True)
        star = Printer.objects.create(name='Star', kind='receipt', host='192.0.2.11', active=True)
        url = f'/tags/furniture-sets/{tag_set.token}/print/'
        self.assertEqual(self.client.post(url, {'printer': star.pk}).status_code, 400)
        with mock.patch('reports.direct_print.send_letter') as send:
            response = self.client.post(url, {'printer': brother.pk, 'copies': '2'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(send.call_args.args[1]), 2)
        self.assertIn('Sent 2 pages to West Counter Brother', response.json()['message'])

    def test_set_tags_default_to_the_color_brother_then_remember_the_last_choice(self):
        self.save()
        tag_set = FurnitureSet.objects.get()
        west = Printer.objects.create(name='West Counter Brother', kind='letter', host='192.0.2.10', active=True)
        office = Printer.objects.create(name='Brother MFC Office', kind='letter', host='192.0.2.12', active=True, color=True)
        page = f'/tags/furniture-sets/{tag_set.token}/'
        self.assertContains(self.client.get(page), f'<option value="{office.pk}" selected>Brother MFC Office</option>', html=True)
        with mock.patch('reports.direct_print.send_letter'):
            self.client.post(f'/tags/furniture-sets/{tag_set.token}/print/', {'printer': west.pk, 'copies': '1'})
        self.assertContains(self.client.get(page), f'<option value="{west.pk}" selected>West Counter Brother</option>', html=True)

    def test_set_tag_fonts_are_twenty_percent_larger(self):
        from reportlab.pdfgen.canvas import Canvas
        from tags import sets
        self.save()
        tag_set = FurnitureSet.objects.get()
        sizes, real = [], Canvas.setFont
        def spy(canvas, name, size, *args, **kwargs):
            sizes.append(round(size, 2))
            return real(canvas, name, size, *args, **kwargs)
        with mock.patch.object(Canvas, 'setFont', spy), mock.patch.object(sets, 'testing_marks', return_value=False):
            sets.pdf_bytes(tag_set)
        self.assertEqual(sets.SET_FONT_SCALE, 1.2)
        self.assertIn(11.4, sizes)    # body text 9.5 -> 11.4
        self.assertIn(12.0, sizes)    # headings and SET TOTAL 10 -> 12
        self.assertIn(18.0, sizes)    # green tagline 15 -> 18
        self.assertNotIn(9.5, sizes)
