# SPDX-License-Identifier: AGPL-3.0-or-later
"""on a tax-exempt sale the lines' Tax % shows 0 (register and saved receipt). Display only."""
from django.test import TestCase


class ExemptLineRateDisplayTests(TestCase):
    def test_register_script_shows_zero_rate_on_exempt_sales(self):
        page = self.client.get('/pos/sale/new/').content.decode()
        # editable lines: the Tax % box shows 0 and is read-only while the sale is exempt
        self.assertIn("if(field==='tax_rate'&&state.tax_exempt===true){input.value='0';input.readOnly=true;", page)
        # read-only (locked / imported) receipts print 0 in the Tax % cell
        self.assertIn("(state.tax_exempt===true?'0':line.tax_rate===null?'—':String(Number(line.tax_rate)))", page)
        # ticking / unticking Tax-exempt redraws the lines, so the line's own rate comes back
        self.assertIn("state.tax_exempt=$('tax-exempt').checked;dirty=true;renderLines();totals();", page)
