# SPDX-License-Identifier: AGPL-3.0-or-later
import re
from unittest import mock

from django.test import TestCase, override_settings

from accounts.signin_helpers import sign_in_admin
from freedomshop.__version__ import __version__
from shop_settings import config
from shop_settings.tests import FULL_MIDDLEWARE

LINKS = ('https://freedomshop.app', 'https://github.com/CyberJody/FreedomShop', 'https://techquest.guru')


class AboutPageTests(TestCase):
    def setUp(self):
        config.clear_cache()
        self.addCleanup(config.clear_cache)

    def test_about_names_product_version_license_credit_and_links(self):
        response = self.client.get('/about/')
        self.assertContains(response, 'FreedomShop <span>Point of Sale</span>', html=False)
        self.assertContains(response, f'<dd id="about-version">{__version__}</dd>')
        self.assertContains(response, 'AGPL-3.0-or-later')
        self.assertContains(response, 'href="/about/license/"')
        self.assertContains(response, 'Created by Jody Grafals / TechQuest')
        for link in LINKS:
            self.assertContains(response, f'href="{link}"')
        self.assertContains(response, '<h2 id="free-help">Free help</h2>')
        self.assertContains(response, '<h2 id="paid-help">Paid help from TechQuest</h2>')
        self.assertNotIn('$', response.content.decode())
        self.assertIsNone(re.search(r'\b(price|pricing|per month|per year|/mo)\b', response.content.decode(), re.I))

    def test_license_text_is_served(self):
        response = self.client.get('/about/license/')
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertContains(response, 'GNU AFFERO GENERAL PUBLIC LICENSE')

    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=True)
    def test_open_from_the_login_page_and_the_file_menu(self):
        login = self.client.get('/login/')
        self.assertContains(login, '<a href="/about/">About FreedomShop</a>')
        for path in ('/about/', '/about/license/'):
            self.assertEqual(self.client.get(path).status_code, 200)
            # Also reachable on a new installation, before first-run setup is finished.
            with self.settings(MIDDLEWARE=FULL_MIDDLEWARE + ['shop_settings.middleware.SetupGateMiddleware']), \
                    mock.patch('shop_settings.middleware.setup_done', return_value=False):
                self.assertEqual(self.client.get(path).status_code, 200)
        sign_in_admin(self.client)
        self.assertContains(self.client.get('/'), '<a href="/about/">About FreedomShop</a>')
