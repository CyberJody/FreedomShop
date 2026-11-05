# SPDX-License-Identifier: AGPL-3.0-or-later
"""New labels follow the saved prefix; old labels remain usable after it changes."""
from django.core.exceptions import ValidationError
from django.test import override_settings

from accounts.signin_helpers import sign_in_admin
from purchasing import piece_labels
from purchasing.piece_codes import check_char, new_code, parse_piece_code
from purchasing.test_piece_labels import LabelBase
from . import config
from .models import Business
from .test_business import form_data
from .tests import FULL_MIDDLEWARE, SettingsCase


class SerialPrefixTests(SettingsCase):
    def test_default_and_validation(self):
        self.assertEqual(Business().serial_prefix, 'FS')
        for prefix in ('FS', 'A2B', '9XYZ', '00'):
            Business(serial_prefix=prefix).full_clean()
        for prefix in ('', 'A', 'ABCDE', 'ab', 'A_', 'A-B', 'AB\n', 'ÄB'):
            with self.subTest(prefix=prefix), self.assertRaises(ValidationError):
                Business(serial_prefix=prefix).full_clean()

    def test_generation_reads_the_saved_prefix(self):
        shop = config.business()
        for prefix in ('FS', 'A2B', '9XYZ'):
            shop.serial_prefix = prefix
            shop.save()
            code = new_code()
            self.assertTrue(code.startswith(prefix + '-'))
            self.assertEqual(parse_piece_code(code), code)

    def test_parser_keeps_any_valid_prefix_independently_of_settings(self):
        body = '2345678'
        chars = body + check_char(body)
        for prefix in ('WC', 'FS', 'A2B', '9XYZ', '00'):
            code = f'{prefix}-{chars[:4]}-{chars[4:]}'
            for value in (code, code.lower(), code.replace('-', ''), f'https://example.com/?wc={code}'):
                self.assertEqual(parse_piece_code(value), code)
        for prefix in ('A', 'ABCDE', 'A_', 'ÄB'):
            self.assertIsNone(parse_piece_code(f'{prefix}-{chars[:4]}-{chars[4:]}'))

    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=True)
    def test_screen_validates_and_saves_the_prefix(self):
        sign_in_admin(self.client)
        shop = config.business()
        self.assertContains(self.client.get('/settings/business/'), 'name="serial_prefix"')
        self.assertEqual(self.client.post('/settings/business/', form_data(shop, serial_prefix='bad')).status_code, 400)
        shop.refresh_from_db()
        self.assertEqual(shop.serial_prefix, 'FS')
        self.assertEqual(self.client.post('/settings/business/', form_data(shop, serial_prefix='D3MO')).status_code, 302)
        self.assertEqual(config.business().serial_prefix, 'D3MO')


class PrefixStorageTests(LabelBase):
    def setUp(self):
        config.clear_cache()
        self.addCleanup(config.clear_cache)
        super().setUp()

    def test_existing_codes_stay_unchanged_and_only_new_pieces_use_new_prefix(self):
        shop = config.business()
        shop.serial_prefix = 'WC'
        shop.save()
        before = {label['code'] for label in piece_labels.labels_for_print(self.po)}
        shop.serial_prefix = 'D3MO'
        shop.save()
        self.assertEqual({label['code'] for label in piece_labels.labels_for_print(self.po)}, before)
        grown = self.save([(self.chair, 7), (self.table, 2)], self.po)
        after = {label['code'] for label in piece_labels.labels_for_print(grown)}
        self.assertTrue(before < after)
        self.assertTrue(all(code.startswith('WC-') for code in before))
        self.assertTrue(all(code.startswith('D3MO-') for code in after - before))
        self.assertTrue(all(parse_piece_code(code) == code for code in after))
