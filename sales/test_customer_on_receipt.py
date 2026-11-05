# SPDX-License-Identifier: AGPL-3.0-or-later
"""name, every phone and the address in the receipt BILL TO block; the register's missing-contact prompt."""
import json
import uuid
from unittest import mock

from django.test import TestCase

from customers.models import Customer
from .letter_printing import Pages, customer_info_lines, render_sales_order
from .models import Document, Item
from .printing import Receipt, bill_to, render_receipt
from .services import save_document


def customer(**fields):
    values = dict(source_id='r39-' + uuid.uuid4().hex, first_name='Pat', last_name='Buyer', full_name='Pat Buyer')
    values.update(fields)
    return Customer.objects.create(**values)


def receipt_for(c, name=None):
    return Document.objects.create(kind='sale', status='held', customer=c,
                                   customer_name=name if name is not None else (c.display_name if c else ''))


ADDRESS = dict(street='12 Bay Rd\nSuite 4', city='Springfield', state='NY', postal_code='62701')


class BillTo(TestCase):
    def test_no_phone_with_address(self):
        doc = receipt_for(customer(**ADDRESS))
        self.assertEqual(bill_to(doc), 'Pat Buyer\n12 Bay Rd\nSuite 4\nSpringfield NY 62701')

    def test_one_phone_no_address(self):
        doc = receipt_for(customer(mobile='518-555-0101'))
        self.assertEqual(bill_to(doc), 'Pat Buyer\n518-555-0101')

    def test_four_phones_in_order_after_the_address(self):
        doc = receipt_for(customer(phone='(518) 555-0100', mobile='518-555-0101', alternate_phone='518 555 0102',
                                   alternate_phone_2='555-0103', **ADDRESS))
        self.assertEqual(bill_to(doc).splitlines(), ['Pat Buyer', '12 Bay Rd', 'Suite 4', 'Springfield NY 62701',
                                                     '(518) 555-0100', '518-555-0101', '518 555 0102', '555-0103'])

    def test_duplicate_phones_print_once(self):
        c = customer(phone='518-555-0102', mobile='(518) 555-0102', alternate_phone=' ', alternate_phone_2='5185550102')
        self.assertEqual(c.phone_numbers(), ['518-555-0102'])
        self.assertEqual(bill_to(receipt_for(c)), 'Pat Buyer\n518-555-0102')

    def test_values_that_are_not_phone_numbers_are_left_off(self):
        # QB's 4th phone field mostly holds 5-digit numbers or first names; a few phone fields hold emails.
        c = customer(phone='UNLISTED', mobile='JO@EXAMPLE.COM', alternate_phone='518-555-0102', alternate_phone_2='18271')
        self.assertEqual(bill_to(receipt_for(c)), 'Pat Buyer\n518-555-0102')
        self.assertEqual(customer(alternate_phone_2='LIZ').phone_numbers(), [])

    def test_city_only_address(self):
        self.assertEqual(bill_to(receipt_for(customer(city='Glens Falls', phone='555-0100'))), 'Pat Buyer\nGlens Falls\n555-0100')

    def test_unlinked_receipt_prints_the_typed_name_only(self):
        self.assertEqual(bill_to(receipt_for(None, name='Walk In Smith')), 'Walk In Smith')

    def test_every_receipt_render_carries_the_block(self):
        c = customer(phone='518-555-0100', mobile='518-555-0101', email='pat@example.com', **ADDRESS)
        item = Item.objects.create(sku='R39', name='Chair', price=10, on_hand=5, tax_rate=7)
        sale = save_document(dict(action='complete', customer_id=c.pk, lines=[dict(item_id=item.pk, quantity='1', price='10', tax_rate='7', description='Test item')],
                                  payments=[dict(method='cash', amount='10.70')]), 'sale', uuid.uuid4())
        held = save_document(dict(action='hold', customer_id=c.pk, lines=[dict(item_id=item.pk, quantity='1', price='10', tax_rate='7', description='Test item')]),
                             'sale', uuid.uuid4())
        for doc, reprint in [(sale, False), (sale, True), (held, False)]:
            with mock.patch.object(Receipt, 'wrap', autospec=True) as wrap:
                render_receipt(doc, reprint=reprint)
            blocks = [call.args[1] for call in wrap.call_args_list]
            self.assertIn('Pat Buyer\n12 Bay Rd\nSuite 4\nSpringfield NY 62701\n518-555-0100\n518-555-0101', blocks)
            self.assertNotIn('pat@example.com', ' '.join(str(b) for b in blocks))


class LetterCustomerInfo(TestCase):
    def test_sales_order_lists_every_phone_once_and_keeps_email(self):
        c = customer(phone='518-555-0100', mobile='(518) 555-0100', alternate_phone='518-555-0102', email='pat@example.com', **ADDRESS)
        order = Document.objects.create(kind='order', status='open', customer=c, customer_name='Pat Buyer')
        self.assertEqual(customer_info_lines(order), ['Pat Buyer', '12 Bay Rd', 'Suite 4', 'Springfield NY 62701',
                                                      '518-555-0100', '518-555-0102', 'pat@example.com'])
        with mock.patch.object(Pages, 'images', autospec=True, side_effect=lambda self: self.pages):
            pages = render_sales_order(order)
        texts = [step[3] for page in pages for step in page if step[0] == 'text']
        self.assertIn('518-555-0102', texts)
        self.assertEqual(texts.count('518-555-0100') + texts.count('(518) 555-0100'), 1)


class LookupFlags(TestCase):
    def test_lookup_reports_missing_phone_and_address(self):
        customer(first_name='Nophone', last_name='Noaddress', full_name='Nophone Noaddress')
        customer(first_name='Complete', last_name='Person', full_name='Complete Person', alternate_phone_2='555-0100', city='Lake George')
        bare = self.client.get('/pos/lookup/customers/?q=Nophone').json()['results'][0]['contact']
        self.assertEqual((bare['missing_phone'], bare['missing_address'], bare['phones']), (True, True, []))
        self.assertEqual(bare['version'], 1)
        full = self.client.get('/pos/lookup/customers/?q=Complete').json()['results'][0]['contact']
        self.assertEqual((full['missing_phone'], full['missing_address'], full['phones'], full['city']), (False, False, ['555-0100'], 'Lake George'))

    def test_register_page_has_the_prompt_only_on_editable_documents(self):
        self.assertContains(self.client.get('/pos/sale/new/'), 'id="contact-dialog"')
        self.assertContains(self.client.get('/pos/order/new/'), 'id="contact-dialog"')
        done = Document.objects.create(kind='order', status='cancelled', customer_name='X')
        self.assertNotContains(self.client.get(f'/pos/order/{done.token}/'), 'id="contact-dialog"')
        imported = Document.objects.create(kind='order', status='completed', customer_name='X', source_id='qb-1')
        self.assertNotContains(self.client.get(f'/pos/order/{imported.token}/'), 'id="contact-dialog"')
        # An OPEN order from QuickBooks is editable, so it gets the prompt like an app order.
        workable = Document.objects.create(kind='order', status='open', customer_name='X', source_id='qb-2')
        self.assertContains(self.client.get(f'/pos/order/{workable.token}/'), 'id="contact-dialog"')


class ContactSave(TestCase):
    def post(self, c, **body):
        return self.client.post(f'/customers/{c.pk}/contact/', json.dumps(body), content_type='application/json')

    def test_save_fills_missing_parts_as_a_local_edit(self):
        c = customer()
        response = self.post(c, version=1, phone=' 518-555-0199 ', street='3 Elm St', city='Springfield', state='NY', postal_code='62701')
        self.assertEqual(response.status_code, 200)
        c.refresh_from_db()
        self.assertEqual((c.phone, c.street, c.city, c.state, c.postal_code), ('518-555-0199', '3 Elm St', 'Springfield', 'NY', '62701'))
        self.assertEqual(c.version, 2)
        self.assertIsNotNone(c.locally_edited_at)
        info = response.json()['customer']['contact']
        self.assertEqual((info['missing_phone'], info['missing_address'], info['version']), (False, False, 2))
        self.assertEqual(bill_to(receipt_for(c)), 'Pat Buyer\n3 Elm St\nSpringfield NY 62701\n518-555-0199')

    def test_phone_only(self):
        c = customer(**ADDRESS)
        self.assertEqual(self.post(c, version=1, phone='5185550199').status_code, 200)
        c.refresh_from_db()
        self.assertEqual((c.phone, c.street, c.version), ('5185550199', ADDRESS['street'], 2))

    def test_refuses_bad_input_and_leaves_the_customer_alone(self):
        c = customer()
        for body, status in [(dict(version=1), 400),                                        # nothing entered
                             (dict(version=1, phone='12'), 400),                            # not a phone
                             (dict(version=1, street='3 Elm St'), 400),                     # no city
                             (dict(version=1, city='x' * 201, street='a'), 400),            # too long
                             (dict(version=1, phone=5551234567), 400),                      # not text
                             (dict(version=7, phone='518-555-0199'), 409)]:                 # stale version
            self.assertEqual(self.post(c, **body).status_code, status, body)
        self.assertEqual(self.client.post(f'/customers/{c.pk}/contact/', 'not json', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.get(f'/customers/{c.pk}/contact/').status_code, 405)
        self.assertEqual(self.post(Customer(pk=999999), version=1, phone='518-555-0199').status_code, 404)
        c.refresh_from_db()
        self.assertEqual((c.phone, c.street, c.city, c.version, c.locally_edited_at), ('', '', '', 1, None))

    def test_never_overwrites_what_is_on_file(self):
        c = customer(mobile='518-555-0101', **ADDRESS)
        self.assertEqual(self.post(c, version=1, phone='518-555-0199', street='9 Other', city='Elsewhere').status_code, 409)
        c.refresh_from_db()
        self.assertEqual((c.phone, c.street, c.version), ('', ADDRESS['street'], 1))

    def test_skip_saves_nothing(self):
        # Skip is client-side only: the register sells to the customer as is and never calls the contact endpoint.
        c = customer()
        item = Item.objects.create(sku='R39S', name='Lamp', price=10, on_hand=5, tax_rate=7)
        save_document(dict(action='complete', customer_id=c.pk, lines=[dict(item_id=item.pk, quantity='1', price='10', tax_rate='7', description='Test item')],
                           payments=[dict(method='cash', amount='10.70')]), 'sale', uuid.uuid4())
        c.refresh_from_db()
        self.assertEqual((c.phone, c.street, c.version, c.locally_edited_at), ('', '', 1, None))

    def test_customer_edit_still_bumps_version_through_the_shared_save(self):
        from customers.forms import CustomerForm
        c = customer(phone='518-555-0100')
        data = {f: getattr(c, f) for f in CustomerForm.Meta.fields if f not in ('tax_rate',)}
        data.update(version=1, city='Lake George', account_limit='', tax_rate='')
        data = {k: v for k, v in data.items() if v not in (False, None)}
        response = self.client.post(f'/customers/{c.pk}/edit/', data)
        self.assertEqual(response.status_code, 302)
        c.refresh_from_db()
        self.assertEqual((c.city, c.version), ('Lake George', 2))
        self.assertIsNotNone(c.locally_edited_at)
