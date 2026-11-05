# SPDX-License-Identifier: AGPL-3.0-or-later
"""a completed receipt prints 2 identical copies on the receipt printer (Save & Print and Reprint); Letter paper,
Create PDF and held receipts stay at 1."""
import uuid
from unittest import mock

from django.test import TestCase

from customers.models import Customer
from .models import Item, Printer
from .printing import receipt_copies, render_receipt, star_raster
from .services import save_document
from .test_printing import FakeConnection, FakeIPP, decode_pwg, ipp_reply

END = b'\x1b\x0c\x00\x1b*rB'    # execute form feed (the cut) + end raster: closes every printed copy
START = b'\x1b@\x1b*rR\x1b*rA'  # init + raster start: opens every copy


class ReceiptTwoCopies(TestCase):
    def setUp(self):
        self.star = Printer.objects.create(name='TEST Receipt Printer', kind='receipt', host='192.0.2.10', port=9100)
        self.brother = Printer.objects.create(name='TEST Brother', kind='letter', host='192.0.2.40', port=631)
        self.customer = Customer.objects.create(source_id='C-R75', full_name='TEST R75 CUSTOMER')
        self.item = Item.objects.create(sku='7501', name='TEST R75 LAMP', description='TEST R75 LAMP', price='54.95', on_hand='5', tax_rate='7.000')

    def receipt(self, action='complete', lines=None):
        data = dict(action=action, customer_id=self.customer.pk, lines=lines or [dict(item_id=self.item.pk, quantity='1', price='54.95', tax_rate='7')])
        if action == 'complete':
            data['payments'] = [dict(method='cash', amount='60.00')]
        return save_document(data, 'sale', uuid.uuid4())

    def copies_sent(self, sent, doc):
        """The job is the one-receipt job repeated: each copy opens and closes (cut) on its own."""
        one = star_raster(render_receipt(doc, reprint=True))
        count = sent.count(END)
        self.assertEqual(sent.count(START), count)
        self.assertEqual(sent, one * count)
        return count

    def test_reprint_and_save_and_print_send_two_cut_copies_in_one_job(self):
        doc = self.receipt()
        conn = FakeConnection()
        with mock.patch('sales.printing.socket.create_connection', return_value=conn) as connect:
            response = self.client.post(f'/pos/sale/{doc.token}/print-receipt/', {'printer': self.star.pk})
        self.assertEqual(response.status_code, 200, response.content)
        connect.assert_called_once()  # one job, one connection
        self.assertEqual(self.copies_sent(conn.sent, doc), 2)

    def test_returns_and_pickup_style_receipts_print_two_and_held_prints_one(self):
        sale = self.receipt()
        ret = save_document(dict(action='complete', customer_id=self.customer.pk,
                                 lines=[dict(returned_from_line=sale.lines.get().pk, item_id=self.item.pk, quantity='-1', price='54.95')],
                                 payments=[dict(method='cash', amount='-58.80')]), 'sale', uuid.uuid4())
        self.assertEqual((ret.status, receipt_copies(ret)), ('completed', 2))
        held = self.receipt(action='hold')
        self.assertEqual(receipt_copies(held), 1)
        # The generic print path (Sales History / imported receipts) follows the same rule.
        for doc, expected in ((ret, 2), (held, 1)):
            conn = FakeConnection()
            with mock.patch('sales.printing.socket.create_connection', return_value=conn):
                response = self.client.post(f'/print/sale/{doc.token}/', {'target': self.star.pk})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(conn.sent.count(END), expected, doc.status)

    def test_pickup_receipt_script_does_not_stop_on_the_hidden_payment_field(self):
        # A pickup receipt's #payment-method is a hidden input; looping over its (missing) options threw at page load,
        # so the page's Print Receipt button never got its handler (found while verifying on the preview).
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'templates/sales/editor-script.html').read_text()
        self.assertIn("forEach(select=>{if(!select.options)return;for(const o of select.options)", source)
        self.assertIn('<input type="hidden" id="payment-method" value="order">',
                      (Path(__file__).resolve().parents[1] / 'templates/sales/editor.html').read_text())

    def test_letter_printer_and_pdf_stay_one_copy(self):
        doc = self.receipt()
        sent = {}

        def urlopen(request, timeout):
            sent['body'] = request.data
            return FakeIPP(ipp_reply())
        with mock.patch('sales.letter_printing.urllib.request.urlopen', side_effect=urlopen):
            self.assertEqual(self.client.post(f'/print/sale/{doc.token}/', {'target': self.brother.pk}).status_code, 200)
        body = sent['body']
        pages = decode_pwg(body[body.index(b'RaS2'):])
        self.assertEqual(len(pages), 1)
        pdf = self.client.get(f'/print/sale/{doc.token}/pdf/').content
        self.assertEqual(pdf.count(b'/Type /Page') - pdf.count(b'/Type /Pages'), 1)
