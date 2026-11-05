# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): the register's Charge Account box shows Available Credit = Account Limit - Balance ('No limit'
when the limit is 0)."""
import json
import re

from django.test import SimpleTestCase

from .test_qb_half_up import SCRIPT, run_node

FAKE_DOM = """
const els={};const $=id=>(els[id]=els[id]||{id,textContent:'',hidden:true});
"""


class AvailableCreditTests(SimpleTestCase):
    def available(self, limit_cents, before_cents):
        source = SCRIPT.read_text()
        money = re.search(r"const money = cents => [^\n]+", source).group()
        line = re.search(r"\$\('acct-available'\)\.textContent=[^\n]+", source).group()
        out = run_node(FAKE_DOM + money + f"\nconst s={{limit:{limit_cents},before:{before_cents}}};\n" + line
                       + "\nconsole.log(JSON.stringify($('acct-available').textContent));")
        return json.loads(out)

    def test_limit_minus_balance(self):
        self.assertEqual(self.available(100000, 19623), '$803.77')   # $1,000 limit, $196.23 owed

    def test_over_the_limit_shows_negative(self):
        self.assertEqual(self.available(5880, 10000), '-$41.20')

    def test_no_limit(self):
        self.assertEqual(self.available(0, 196230), 'No limit')     # it used to be blank
