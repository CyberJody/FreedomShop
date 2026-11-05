# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Mode = signed in as an admin. No File-menu switch, no /admin-mode/ route; the shared
register login and PIN cashiers never reach Reports or Server Status. Sign In / Sign Out sit in the menubar and the
File menu. TEST users only."""
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase, override_settings

from .cashiers import SESSION_KEY as CASHIER_KEY
from .models import Cashier
from .signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register

FULL_MIDDLEWARE = ['django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
                   'django.contrib.auth.middleware.AuthenticationMiddleware', 'accounts.middleware.RequireLoginMiddleware']
ADMIN_URLS = ['/reports/', '/reports/sales-by-vendor/', '/reports/sales-tax/', '/reports/general-sales/',
              '/reports/inventory-on-hand/', '/reports/daily-totals/', '/reports/customer-balances/', '/server-status/']
PRINT_URLS = ['/reports/sales-by-vendor/print/', '/reports/sales-tax/print/', '/reports/daily-totals/print/']


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class AdminModeTests(TestCase):
    def assert_not_admin(self):
        home = self.client.get('/')
        self.assertNotContains(home, 'href="/reports/"')
        self.assertNotContains(home, 'aria-label="Reports"')
        self.assertNotContains(home, 'href="/server-status/"')
        self.assertNotContains(home, 'class="admin-mode-badge')
        self.assertNotContains(self.client.get('/customers/'), 'href="/reports/"')
        self.assertNotContains(self.client.get('/pos/history/'), 'href="/reports/"')
        for url in ADMIN_URLS:
            self.assertRedirects(self.client.get(url), '/?admin=off', fetch_redirect_response=False)
        for url in PRINT_URLS:
            response = self.client.post(url, {'printer': '1'})
            self.assertEqual((response.status_code, response.json()['error']), (403, 'Sign in as an admin to use this.'))
        self.assertContains(self.client.get('/?admin=off'), 'That screen is for admins. Sign in as an admin to open it')

    def test_file_menu_has_no_admin_mode_switch(self):
        for sign_in in (None, sign_in_register, sign_in_admin):
            if sign_in:
                sign_in(self.client)
            for url in ['/', '/customers/', '/pos/history/']:
                page = self.client.get(url)
                self.assertNotContains(page, '/admin-mode/')
                self.assertNotContains(page, 'Admin Mode (on)')
                self.assertNotContains(page, '>Admin Mode<')
                self.assertNotContains(page, 'admin-mode-form')

    def test_admin_mode_route_is_gone_for_everyone(self):
        for sign_in in (None, sign_in_register, sign_in_admin):
            if sign_in:
                sign_in(self.client)
            self.assertEqual(self.client.get('/admin-mode/').status_code, 404)
            self.assertEqual(self.client.post('/admin-mode/', HTTP_ACCEPT='application/json').status_code, 404)
            self.assertNotIn('admin_mode', self.client.session)

    def test_nobody_signed_in_is_not_admin(self):
        self.assert_not_admin()

    def test_register_login_is_never_admin_even_staff_or_with_old_session_flag(self):
        user = sign_in_register(self.client)
        user.is_staff = user.is_superuser = True
        user.save()
        session = self.client.session
        session['admin_mode'] = True   # the retired File-menu flag no longer means anything
        session.save()
        self.assert_not_admin()

    def test_pin_cashier_at_the_register_is_not_admin(self):
        sign_in_register(self.client)
        cashier = Cashier.objects.create(name='TEST Cashier', pin_hash=make_password('0123'))
        session = self.client.session
        session[CASHIER_KEY] = cashier.pk
        session.save()
        self.assert_not_admin()

    def test_admin_sign_in_is_admin_mode(self):
        sign_in_admin(self.client)
        home = self.client.get('/')
        self.assertContains(home, 'href="/reports/"')
        self.assertContains(home, 'aria-label="Reports"')
        self.assertContains(home, 'ADMIN MODE')
        self.assertContains(home, '<a href="/server-status/">Server Status</a>')   # File menu
        self.assertContains(self.client.get('/pos/history/'), 'href="/reports/"')
        for url in ['/reports/', '/reports/sales-tax/', '/reports/daily-totals/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertNotContains(self.client.get('/?admin=off'), 'That screen is for admins')
        # Signing out ends it.
        self.client.logout()
        self.assertRedirects(self.client.get('/reports/'), '/?admin=off', fetch_redirect_response=False)

    def test_inactive_admin_is_not_admin(self):
        user = sign_in_admin(self.client)
        user.is_active = False
        user.save()
        self.assert_not_admin()


class SignInOutLinkTests(TestCase):
    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=False)
    def test_sign_in_link_when_signed_out_goes_to_login(self):
        home = self.client.get('/customers/')
        self.assertContains(home, '<a class="menu-link account-link" href="/login/?next=/customers/">Sign In</a>')
        self.assertContains(home, '<a href="/login/?next=/customers/">Sign In</a>')   # File menu too
        self.assertNotContains(home, 'Sign Out')
        login = self.client.get('/login/?next=/customers/')
        self.assertContains(login, 'Welcome to FreedomShop')
        self.assertContains(login, 'Demo Home &amp; Hardware')
        self.assertContains(login, 'value="/customers/"')

    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=True)
    def test_sign_out_shows_when_signed_in_and_signs_out_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertRedirects(client.get('/'), '/login/?next=/', fetch_redirect_response=False)   # preview opens on login
        sign_in_register(client)
        home = client.get('/')
        body = home.content.decode()
        self.assertNotContains(home, '>Sign In<')
        self.assertEqual(body.count('<form method="post" action="/logout/"'), 2)   # menubar + File menu
        self.assertIn('class="account-link"', body)
        self.assertIn('Signed in as TEST Register', body)
        self.assertEqual(client.post('/logout/').status_code, 403)   # no CSRF token
        token = client.cookies['csrftoken'].value
        response = client.post('/logout/', {'csrfmiddlewaretoken': token})
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assertRedirects(client.get('/'), '/login/?next=/', fetch_redirect_response=False)

    @override_settings(MIDDLEWARE=FULL_MIDDLEWARE, POS_REQUIRE_LOGIN=True)
    def test_sign_in_and_out_on_register_and_customer_pages(self):
        sign_in_admin(self.client)
        for url in ['/pos/history/', '/customers/']:
            self.assertContains(self.client.get(url), 'class="account-link"')


@override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
class ServerStatusTests(TestCase):
    def on(self):
        sign_in_admin(self.client)

    def test_hidden_and_refused_when_off(self):
        home = self.client.get('/')
        self.assertNotContains(home, '/server-status/')
        self.assertNotContains(home, 'class="admin-mode-badge')
        self.assertRedirects(self.client.get('/server-status/'), '/?admin=off', fetch_redirect_response=False)

    def test_placement_when_on(self):
        self.on()
        home = self.client.get('/')
        # Reports tile (last), File menu item, and the badge links to it; no sidebar shortcut.
        self.assertContains(home, 'href="/server-status/"')
        self.assertNotContains(home, 'class="shortcut shortcut-admin"')
        self.assertContains(home, '<use href="#server-status"/>')
        tiles = home.content.decode().split('aria-label="Reports"')[1]
        self.assertLess(tiles.index('All Reports'), tiles.index('Server Status'))
        self.assertContains(home, '<a href="/server-status/">Server Status</a>')   # File menu
        self.assertContains(home, 'admin-mode-badge')
        self.assertContains(home, '<span class="health ')
        reports = self.client.get('/reports/')
        rows = reports.content.decode()
        self.assertLess(rows.rindex('Inventory On Hand'), rows.rindex('Server Status'))

    def test_page_when_on(self):
        self.on()
        response = self.client.get('/server-status/')
        self.assertEqual(response.status_code, 200)
        for title in ['Backups', 'RAID &amp; drives', 'System', 'Memory', 'Storage', 'Software', 'Application']:
            self.assertContains(response, f'<h2>{title}</h2>')
        body = response.content.decode()
        self.assertLess(body.index('<h2>Backups</h2>'), body.index('<h2>RAID &amp; drives</h2>'))
        self.assertLess(body.index('<h2>RAID &amp; drives</h2>'), body.index('<h2>System</h2>'))
        self.assertContains(response, 'Django')
        self.assertContains(response, 'Operating system')
        self.assertContains(response, 'Nightly')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_gather_never_raises_and_reports_states(self):
        from accounts import server_status
        sections = server_status.gather()
        self.assertEqual([s.title for s in sections], ['Power (UPS)', 'Backups', 'RAID & drives', 'System', 'Memory', 'Storage', 'Software', 'Application'])
        for s in sections:
            for i in s.items:
                self.assertIn(i.state, {'ok', 'warn', 'bad', 'muted'})
                self.assertTrue(i.label and i.value)
        state, summary = server_status.overall(sections)
        self.assertIn(state, {'ok', 'warn', 'bad'})
        # Software facts we can assert regardless of host
        software = {i.label: i.value for i in [s for s in sections if s.title == 'Software'][0].items}
        import django
        self.assertEqual(software['Django'], django.get_version())
        self.assertEqual(software['Python'], '.'.join(map(str, __import__('sys').version_info[:3])))

    def test_health_dot_reflects_state_and_is_cached(self):
        from django.core.cache import cache
        from accounts import server_status
        self.on()
        cache.delete(server_status.CACHE_KEY)
        with mock.patch.object(server_status, 'gather', return_value=[server_status.Section('X', [server_status.Item('Drive /dev/sda', 'FAILED', 'bad')])]) as gather:
            home = self.client.get('/')
            self.assertContains(home, 'class="health bad"')
            self.assertContains(home, 'admin-mode-badge bad')
            self.client.get('/customers/')
            self.assertEqual(gather.call_count, 1)   # second page served from cache
            page = self.client.get('/server-status/?refresh=1')
            self.assertEqual(gather.call_count, 2)
            self.assertContains(page, 'Attention needed')
        cache.delete(server_status.CACHE_KEY)
        with mock.patch.object(server_status, 'gather', return_value=[server_status.Section('X', [server_status.Item('RAM', 'fine', 'ok')])]):
            self.assertContains(self.client.get('/'), 'class="health ok"')
        cache.delete(server_status.CACHE_KEY)

    def test_permission_denied_paths_and_failing_probes_do_not_crash(self):
        from pathlib import Path
        from accounts import server_status
        with mock.patch.object(Path, 'exists', side_effect=PermissionError('denied')):
            self.assertTrue(server_status._exists(Path('/etc/letsencrypt/live/x/cert.pem')))
        with mock.patch.object(Path, 'exists', side_effect=OSError('boom')):
            self.assertFalse(server_status._exists(Path('/nope')))
        with mock.patch.object(server_status, 'storage_section', side_effect=RuntimeError('probe exploded')):
            sections = server_status.gather()
        storage = [s for s in sections if s.title == 'Storage'][0]
        self.assertEqual(storage.items[0].state, 'warn')
        self.assertIn('probe exploded', storage.items[0].value)

    def test_helpers(self):
        from datetime import timedelta
        from django.utils import timezone
        from accounts import server_status
        self.assertEqual(server_status.human_bytes(512), '512 B')
        self.assertEqual(server_status.human_bytes(1536), '1.5 KB')
        self.assertEqual(server_status.human_bytes(3 * 1024 ** 3), '3.0 GB')
        self.assertEqual(server_status.ago(timezone.now() - timedelta(minutes=5)), '5 min ago')
        self.assertEqual(server_status.ago(timezone.now() - timedelta(days=3)), '3 days ago')
        code, out, err = server_status.run(['definitely-not-a-command-xyz'])
        self.assertIsNone(code)
