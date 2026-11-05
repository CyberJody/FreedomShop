# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
import subprocess
from pathlib import Path
from decimal import Decimal as D
from django.test import SimpleTestCase
from sales.models import money, line_amount, tax_amount, restocking_fees, allocate_receipt_discount, discounted_price, unit_price
from sales.rebuild import whole
import shutil
import unittest


def run_node(script):
    """Run the register's JS in Node. The parity tests run on the dev machine; the store server has no Node, so
    they skip there (the server suite still runs every Python test)."""
    if shutil.which('node') is None:
        raise unittest.SkipTest('node is not installed here; JS parity tests run on the dev machine')
    return subprocess.check_output(['node', '-e', script], text=True)

SCRIPT = Path(__file__).resolve().parents[1] / 'templates/sales/editor-script.html'


def register_math():
    """The register's pure QB-math block (between the markers in editor-script.html), as Node source."""
    source = SCRIPT.read_text()
    start, end = source.index('// === QB math'), source.index('// === end QB math ===')
    return source[start:end]


class QBHalfUpTests(SimpleTestCase):
    def test_extensions_signed_ties_and_recorded_values(self):
        self.assertEqual(line_amount(D('3'),D('0.335')),D('1.01'))
        self.assertEqual(line_amount(D('-3'),D('0.335')),D('-1.01'))
        self.assertEqual(line_amount(D('3'),D('0.33'),D('1.01')),D('1.01'))
        self.assertEqual(whole('0.995'),D('1.00'))

    def test_qb_practice_tax_case_and_negative_refund(self):
        self.assertEqual(tax_amount([(D('75'),D('7.5'))]),D('5.63'))
        self.assertEqual(tax_amount([(D('-75'),D('7.5'))]),D('-5.63'))
        # Per-rate subtotal, not sum of line-by-line rounded tax.
        self.assertEqual(tax_amount([(D('0.05'),D('7'))]*3),D('0.01'))
        self.assertEqual(tax_amount([(D('1'),D('7.5')),(D('1'),D('8.5'))]),D('0.17'))
        # Tax on the unrounded extensions (QB receipts 106279 and 103893): 129.95 + 1 x 109.975 -> 16.79; 2 x 277.4625 -> 38.84.
        self.assertEqual(tax_amount([(D('129.95'),D('7')),(D('109.975'),D('7'))]),D('16.79'))
        self.assertEqual(tax_amount([(D('2')*D('277.4625'),D('7'))]),D('38.84'))

    def test_register_bigint_matches_server_on_both_signs(self):
        fn=re.search(r'function halfUp\(n,d\)\{[^\n]+',SCRIPT.read_text()).group()
        cases=[(7500,7500),(-7500,7500),(5,7000),(-5,7000),(3333,20000),(99,90000)]
        script=fn+'\nconsole.log(JSON.stringify('+json.dumps(cases)+'.map(([n,r])=>Number(halfUp(BigInt(n)*BigInt(r),100000n)))));'
        actual=json.loads(run_node(script))
        expected=[int(money(D(n)*D(rate)/D('10000000'))*100) for n,rate in cases]
        self.assertEqual(actual,expected)

    # Receipts the register must total exactly like the server (and QB). Lines: quantity, price (exact text),
    # tax_rate, optional return flags; expected values are QB's stored figures where a real receipt is cited.
    VECTORS = [
        # QB 121467 (5 % receipt discount, leftover cent on the 10 x 349.95 line): 12,474.15 / 623.71 / 829.53
        dict(name='qb-121467', pct='5', exempt=False, lines=[('1', '1399.95', '7'), ('1', '1449.95', '7'), ('1', '374.95', '7'), ('1', '1349.95', '7'),
                                                                ('2', '599.95', '7'), ('1', '2399.95', '7'), ('1', '800', '7'), ('10', '349.95', '7')]),
        # QB 117922 (50 %): 2,634.80 / 1,317.40 / 92.22
        dict(name='qb-117922', pct='50', exempt=False, lines=[('1', '874.95', '7'), ('1', '264.95', '7'), ('1', '1249.95', '7'), ('1', '244.95', '7')]),
        # QB 106279: half-cent extension, tax on the unrounded amounts: 239.93 / 0 / 16.79
        dict(name='qb-106279', pct='0', exempt=False, lines=[('1', '129.95', '7'), ('1', '109.975', '7'), ('1', '0', '0')]),
        # QB 103893: 2 x 277.4625 = 554.93, tax 38.84
        dict(name='qb-103893', pct='0', exempt=False, lines=[('2', '277.4625', '7')]),
        # QB 121405 (line % only): 4 x 314.955 = 1,259.82 + 1,241.955 -> 2,501.78, tax 175.12
        dict(name='qb-121405', pct='0', exempt=False, lines=[('4', '314.955', '7'), ('1', '1241.955', '7')]),
        # ties: three 0.05 lines at 10 %
        dict(name='ties', pct='10', exempt=False, lines=[('1', '0.05', '7')] * 3),
        # line % and receipt % together, mixed rates and a non-taxable line
        dict(name='mixed', pct='5', exempt=False, lines=[('1', '127.4625', '7'), ('3', '19.99', '0'), ('2', '45.5', '8.875')]),
        # tax-exempt receipt with a discount
        dict(name='exempt', pct='12.5', exempt=True, lines=[('1', '199.95', '7'), ('2', '10.01', '7')]),
        # a return line from a 10 %-discounted receipt (refund the discounted amount) plus a new sale line
        dict(name='return', pct='0', exempt=False, lines=[('-1', '100', '7', dict(returned_from_line=5, return_limit='1', return_discount_percent='10')), ('1', '55.55', '7')]),
    ]

    def server_totals(self, vector):
        amounts, shares, rates, sub = [], [], [], D('0')
        for qty, price, rate, *flags in vector['lines']:
            flag = flags[0] if flags else {}
            amount = line_amount(D(qty), D(price))
            sub += amount
            rates.append(D(rate))
            amounts.append((D(qty) * D(price), amount, flag))
        eligible = [i for i, (_, _, flag) in enumerate(amounts) if not flag.get('returned_from_line')]
        shares = [D('0')] * len(amounts)
        for i, share in zip(eligible, allocate_receipt_discount([amounts[i][1] for i in eligible], D(vector['pct']))):
            shares[i] = share
        for i, (_, amount, flag) in enumerate(amounts):
            if flag.get('returned_from_line'):
                shares[i] = money(amount * D(flag['return_discount_percent']) / 100)
        discount = sum(shares, D('0'))
        tax = tax_amount(((exact - share, rate) for (exact, _, _), share, rate in zip(amounts, shares, rates)), vector['exempt'])
        return dict(sub=int(sub * 100), discount=int(discount * 100), tax=int(tax * 100), total=int((sub - discount + tax) * 100))

    def test_register_totals_match_server_and_qb_receipts(self):
        vectors = []
        for v in self.VECTORS:
            lines = []
            for qty, price, rate, *flags in v['lines']:
                lines.append(dict(quantity=qty, price=price, tax_rate=rate, **(flags[0] if flags else {})))
            vectors.append(dict(lines=lines, pct=v['pct'], exempt=v['exempt']))
        script = (register_math() + '\nconst vectors=' + json.dumps(vectors) + ';'
                  'console.log(JSON.stringify(vectors.map(v=>{const t=qbTotals(v.lines,v.pct,"sale",v.exempt);return {sub:t.sub,discount:t.discount,tax:t.tax,total:t.total};})));')
        actual = json.loads(run_node(script))
        for v, got in zip(self.VECTORS, actual):
            self.assertEqual(got, self.server_totals(v), v['name'])
        by_name = {v['name']: got for v, got in zip(self.VECTORS, actual)}
        self.assertEqual(by_name['qb-121467'], dict(sub=1247415, discount=62371, tax=82953, total=1267997))
        self.assertEqual(by_name['qb-117922'], dict(sub=263480, discount=131740, tax=9222, total=140962))
        self.assertEqual(by_name['qb-106279'], dict(sub=23993, discount=0, tax=1679, total=25672))
        self.assertEqual(by_name['qb-103893'], dict(sub=55493, discount=0, tax=3884, total=59377))
        self.assertEqual(by_name['qb-121405'], dict(sub=250178, discount=0, tax=17512, total=267690))
        # Return of a $100 item from a 10 %-off receipt plus a $55.55 sale: refund basis -90 + 55.55 = -34.45, tax -2.41.
        self.assertEqual(by_name['return'], dict(sub=-4445, discount=-1000, tax=-241, total=-3686))

    def test_register_line_percent_price_matches_server(self):
        cases = [('1379.95', '10'), ('349.95', '10'), ('169.95', '25'), ('184.95', '41.09'), ('1449.95', '5'), ('0.05', '33.33'), ('99.99', '100')]
        script = (register_math() + '\nconst cases=' + json.dumps(cases) + ';'
                  'console.log(JSON.stringify(cases.map(([p,pct])=>[unitsText(discountedUnits(fixed(p,2),fixed(pct,2))),unitsDisplay(discountedUnits(fixed(p,2),fixed(pct,2)))])));')
        actual = json.loads(run_node(script))
        for (price, pct), (exact, shown) in zip(cases, actual):
            expected = discounted_price(D(price), D(pct))
            self.assertEqual(D(exact), expected, (price, pct))
            self.assertEqual(shown, str(money(expected)), (price, pct))
        self.assertEqual(unit_price(D('108.954045')), D('108.9540'))

    def test_line_percent_input_updates_in_place(self):
        # Typing a line % must not re-render the line table (that replaced the box being typed in and dropped focus
        # after the first digit; a prior review). Verified by typing "25" and "12.5" continuously in
        # headless Chrome on the preview (the import parity checks); this guards the handler.
        source = SCRIPT.read_text()
        handler = re.search(r'pct\.oninput=\(\)=>\{[^\n]+', source).group()
        self.assertNotIn('renderLines()', handler)
        self.assertIn("querySelector('.line-price')", handler)
        self.assertIn("querySelector('.line-amount')", handler)
        self.assertIn('totals();', handler)
