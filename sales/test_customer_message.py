# SPDX-License-Identifier: AGPL-3.0-or-later
"""a "choose a customer first" message on the register / order screen goes away once a customer is picked."""
import json
import re

from django.test import SimpleTestCase

from .test_qb_half_up import SCRIPT, run_node

FAKE_DOM = """
const els={};const $=id=>(els[id]=els[id]||{id,textContent:'',hidden:true});
"""


class CustomerMessageClearsTests(SimpleTestCase):
    def run_clear(self, message, hidden=False):
        source = SCRIPT.read_text()
        block = re.search(r"const CUSTOMER_NEEDED=[^\n]+\nfunction clearCustomerError\(\)\{[^\n]+", source).group()
        out = run_node(FAKE_DOM + block + f"\nconst e=$('error');e.textContent={json.dumps(message)};e.hidden={json.dumps(hidden)};"
                       "\nclearCustomerError();console.log(JSON.stringify([e.hidden,e.textContent]));")
        return json.loads(out)

    def test_customer_messages_clear(self):
        for message in ('Choose a customer before using Account.', 'Choose a customer before refunding to Account.',
                        'A customer is required on every receipt and sales order.'):
            with self.subTest(message=message):
                self.assertEqual(self.run_clear(message), [True, ''])

    def test_other_errors_stay(self):
        self.assertEqual(self.run_clear('Enter the price in dollars and cents.'), [False, 'Enter the price in dollars and cents.'])

    def test_select_customer_clears_first(self):
        self.assertIn('function selectCustomer(customer){clearCustomerError();', SCRIPT.read_text())
