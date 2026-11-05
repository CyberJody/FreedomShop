# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): a sale charged to the customer's account says so in the save confirmation."""
import json
import re

from django.test import SimpleTestCase

from .test_qb_half_up import SCRIPT, run_node


class ChargeWordingTests(SimpleTestCase):
    def test_charge_question_whole_and_split(self):
        fn = re.search(r'function chargeQuestion\(total,split,charge\)\{[^\n]+', SCRIPT.read_text()).group()
        out = json.loads(run_node(fn + "\nconsole.log(JSON.stringify([chargeQuestion('$120.00','','$120.00'),"
                                       "chargeQuestion('$120.00','Cash + Account','$70.00')]));"))
        self.assertEqual(out[0], "Save this sale for $120.00 and charge it to the customer's account?")
        self.assertEqual(out[1], "Save this sale for $120.00 (Cash + Account) and charge $70.00 to the customer's account?")

    def test_complete_uses_charge_question_and_no_account_payment_wording(self):
        source = SCRIPT.read_text()
        self.assertIn("charging?`${chargeQuestion(", source)
        self.assertIn("rawMethods.includes('account')&&acct.charge>0", source)
        # The balance lines stay; only the doubled "Charge $X to the account." is dropped when the question says it.
        self.assertIn("Account balance ${money(acct.before)} → ${money(acct.after)}.", source)
