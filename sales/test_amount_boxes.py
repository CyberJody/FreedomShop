# SPDX-License-Identifier: AGPL-3.0-or-later
"""(matching QB): typing in a pre-filled amount box replaces it (select on focus), and the Save question warns
when the change would be more than the sale. Browser behaviour is checked on the preview; these pin the wiring."""
from django.test import TestCase, override_settings

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_register


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class AmountBoxTests(TestCase):
    def setUp(self):
        sign_in_register(self.client)

    def test_number_boxes_select_their_contents_on_focus(self):
        for url in ('/pos/sale/new/', '/pos/order/new/'):
            page = self.client.get(url).content.decode()
            self.assertIn("document.addEventListener('focusin'", page)
            self.assertIn("box.type==='number'", page)
            self.assertIn('.select()', page)
            # the click's mouseup is never cancelled (that kept Chrome's spinner arrows counting); the box is
            # selected again just after the click, unless the click changed the value (an arrow) or dragged.
            self.assertIn("document.addEventListener('mouseup'", page)
            self.assertIn('click.box.value===click.value', page)
            block = page[page.index('let focusClick'):page.index('function renderShippingChoices')]
            self.assertNotIn('preventDefault', block)
            self.assertNotIn('selectedByFocus', page)

    def test_save_question_warns_when_change_is_more_than_the_sale(self):
        page = self.client.get('/pos/sale/new/').content.decode()
        self.assertIn('function changeWarning()', page)
        self.assertIn('Change due ${money(changeCents)}: check the amount received.', page)
        self.assertIn('changeCents>totalCents', page)
        self.assertIn('+big))return;', page)   # appended to the confirm text, never a block
