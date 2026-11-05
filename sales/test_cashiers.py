# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings

from accounts.models import Cashier
from accounts.tests import MIDDLEWARE, make_admin, make_register
from .models import Document
from .tests import item, payload
from .letter_printing import Pages, render_sales_order
from .printing import Receipt, render_receipt


def printed_receipt(sale):
    with patch.object(Receipt, 'wrap', autospec=True) as wrap, patch.object(Receipt, 'text', autospec=True) as text:
        render_receipt(sale)
    return ' '.join(str(call) for call in wrap.call_args_list + text.call_args_list)


def printed_order_has(order, line):
    with patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
        pages = render_sales_order(order)
    return any(step[0] == 'text' and step[3] == line for page in pages for step in page)


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class CashierDocumentTests(TestCase):
    def setUp(self):
        self.user = make_register()
        self.admin = make_admin()
        self.cashier = Cashier.objects.create(name='TEST Cashier', pin_hash=make_password('0123'))
        self.client.force_login(self.user)
        self.product = item()

    def save(self, kind, **changes):
        data = payload(self.product, action='complete' if kind == 'sale' else 'save',
                       payment_method='cash', tendered='20.00', cashier='FORGED')
        data.update(changes)
        response = self.client.post(f'/pos/{kind}/new/', json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return Document.objects.get(token=data['token'])

    def activate(self):
        return self.client.post('/cashier/', {'pin': '0123', 'cashier': self.cashier.pk})

    def receipt_page(self, sale):
        """The receipt's read-only header (shown once the receipt is locked after its day) names the cashier."""
        from datetime import timedelta
        from django.utils import timezone
        Document.objects.filter(pk=sale.pk).update(completed_at=timezone.now() - timedelta(days=2))
        return self.client.get(f'/pos/sale/{sale.token}/')

    def test_receipt_and_order_stamped_from_session_and_printed(self):
        self.activate()
        sale = self.save('sale')
        order = self.save('order')
        self.assertEqual(sale.cashier, 'TEST Cashier')
        self.assertEqual(order.cashier, 'TEST Cashier')
        # the printed receipt never names the salesperson (still recorded on the document).
        printed = printed_receipt(sale)
        self.assertNotIn('TEST Cashier', printed)
        self.assertNotIn('Cashier:', printed)
        self.assertTrue(printed_order_has(order, 'Cashier: TEST Cashier'))
        self.assertContains(self.receipt_page(sale), 'Cashier TEST Cashier')

    def test_admin_is_the_cashier_on_documents_without_a_pin(self):
        """a signed-in admin is credited automatically; same printing rules as a PIN cashier."""
        self.client.force_login(self.admin)
        sale = self.save('sale')
        order = self.save('order')
        self.assertEqual(sale.cashier, 'TEST Admin')
        self.assertEqual(order.cashier, 'TEST Admin')
        printed = printed_receipt(sale)
        self.assertNotIn('TEST Admin', printed)
        self.assertNotIn('Cashier:', printed)
        self.assertTrue(printed_order_has(order, 'Cashier: TEST Admin'))
        self.assertContains(self.receipt_page(sale), 'Cashier TEST Admin')

    def test_no_cashier_does_not_accept_forged_identity(self):
        self.assertEqual(self.save('sale').cashier, '')
        self.assertEqual(self.save('sale', action='hold').cashier, '')
        self.activate()
        self.client.post('/cashier/', {'action': 'clear'})
        self.assertEqual(self.save('order').cashier, '')

    def test_hold_records_who_held_it_and_completion_restamps(self):
        """(matching QB): a held receipt shows who put it on hold; whoever completes it later is stamped then."""
        self.activate()
        held = self.save('sale', action='hold')
        self.assertEqual((held.status, held.cashier), ('held', 'TEST Cashier'))
        rows = self.client.get('/pos/held/?period=all&format=rows&offset=0').json()['rows']
        self.assertIn('TEST Cashier', [row['cells'][5] for row in rows if row['token'] == str(held.token)][0])
        self.assertContains(self.client.get('/pos/held/?period=all'), '<td>TEST Cashier</td>')
        self.assertContains(self.client.get(f'/pos/held/{held.token}/details/'), 'Cashier TEST Cashier')
        # Completed later by an admin (no PIN): the completing person is credited.
        self.client.force_login(self.admin)
        data = payload(self.product, action='complete', payment_method='cash', tendered='20.00')
        data.update(token=str(held.token), version=held.version)
        response = self.client.post(f'/pos/sale/{held.token}/', json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        held.refresh_from_db()
        self.assertEqual((held.status, held.cashier), ('completed', 'TEST Admin'))

    def test_imported_hold_keeps_qb_cashier_unless_reheld_by_an_active_cashier(self):
        """a QB held receipt keeps QB's cashier when nobody is active; an active cashier who re-holds it takes it."""
        from .models import Line
        imported = Document.objects.create(kind='sale', status='held', source_id='QBHELD1', cashier='Morgan', customer_name='',
                                           subtotal='10.00', tax='0.70', total='10.70')
        Line.objects.create(document=imported, position=0, item=self.product, sku=self.product.sku, item_name=self.product.name,
                            quantity=1, price='10.00', tax_rate='7')

        def rehold():
            doc = Document.objects.get(pk=imported.pk)
            data = payload(self.product, action='hold')
            data.update(token=str(doc.token), version=doc.version)
            response = self.client.post(f'/pos/sale/{doc.token}/', json.dumps(data), content_type='application/json')
            self.assertEqual(response.status_code, 200, response.content)
            return Document.objects.get(pk=imported.pk)

        self.assertEqual(rehold().cashier, 'Morgan')  # register login, no PIN cashier: QB's name stays
        self.activate()
        self.assertEqual(rehold().cashier, 'TEST Cashier')

    def test_deactivated_cashier_keeps_name_on_history(self):
        self.activate()
        sale = self.save('sale')
        self.cashier.active = False
        self.cashier.save()
        sale.refresh_from_db()
        self.assertEqual(sale.cashier, 'TEST Cashier')
        page = self.receipt_page(sale)
        self.assertContains(page, 'Cashier TEST Cashier')  # the record keeps the name
        self.assertContains(page, 'Cashier: <span id="cashier-name">No cashier</span>')  # but nobody is active now
        self.assertEqual(self.save('sale').cashier, '')  # no longer active: nothing new is credited to them
