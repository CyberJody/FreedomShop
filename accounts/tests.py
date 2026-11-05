# SPDX-License-Identifier: AGPL-3.0-or-later
"""Login tests. All credentials here are TEST values for TEST users; all identities and credentials are made up."""
from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from .models import LoginWindow, Cashier
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import Group
from django.core.management import call_command, CommandError
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from . import cashiers
from .roles import ADMIN_GROUP, REGISTER_GROUP, login_choices

MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','accounts.middleware.RequireLoginMiddleware']


def make_register(username='register', password='TEST-shared', **fields):
    user = get_user_model().objects.create_user(username, password=password, **fields)
    user.groups.add(Group.objects.get_or_create(name=REGISTER_GROUP)[0])
    return user


def make_admin(username='TEST Admin', password='TEST-admin'):
    user = get_user_model().objects.create_user(username, password=password)
    user.groups.add(Group.objects.get_or_create(name=ADMIN_GROUP)[0])
    return user


@override_settings(MIDDLEWARE=MIDDLEWARE,POS_REQUIRE_LOGIN=True)
class LoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_register('staff',password='Local-testing-only-95!')

    def test_every_business_surface_requires_login_and_logout_ends_session(self):
        for path in ['/','/customers/','/purchasing/items/','/purchasing/items/?format=rows','/pos/history/','/pos/order/new/','/pos/lookup/customers/?q=test']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,302,path);self.assertTrue(response.url.startswith('/login/?next='))
        self.assertEqual(self.client.get('/health/').status_code,200)
        self.assertNotContains(self.client.get('/login/'),'All test changes will be discarded')  # banner removed
        self.assertEqual(self.client.post('/pos/sale/new/','{}',content_type='application/json').status_code,401)
        self.assertEqual(self.client.post('/login/',{'username':'staff','password':'Local-testing-only-95!','next':'https://evil.example/'}).url,'/')
        self.assertContains(self.client.get('/'),'Sign Out')
        self.assertIn('no-store',self.client.get('/customers/')['Cache-Control'])
        self.assertEqual(self.client.post('/logout/').status_code,302)
        self.assertEqual(self.client.get('/customers/').status_code,302)

    def test_login_and_logout_csrf(self):
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/login/',{'username':'staff','password':'Local-testing-only-95!'}).status_code,403)
        client.get('/login/');token=client.cookies['csrftoken'].value
        self.assertEqual(client.post('/login/',{'username':'staff','password':'Local-testing-only-95!','csrfmiddlewaretoken':token}).status_code,302)
        self.assertEqual(client.post('/logout/').status_code,403)
        token=client.cookies['csrftoken'].value
        self.assertEqual(client.post('/logout/',{'csrfmiddlewaretoken':token}).status_code,302)

    def test_shared_failed_login_limit_expires(self):
        for _ in range(10):
            self.assertEqual(self.client.post('/login/',{'username':'staff','password':'wrong'}).status_code,200)
        self.assertEqual(LoginWindow.objects.get().failures,10)
        self.assertContains(self.client.post('/login/',{'username':'staff','password':'Local-testing-only-95!'}),'Too many unsuccessful attempts')
        LoginWindow.objects.update(started_at=timezone.now()-timedelta(minutes=16))
        self.assertEqual(self.client.post('/login/',{'username':'staff','password':'Local-testing-only-95!'}).status_code,302)


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class LoginDropdownTests(TestCase):
    """the sign-in username is a dropdown of admins + the register login; PIN staff never sign in."""
    @classmethod
    def setUpTestData(cls):
        cls.register = make_register()
        cls.admin_b = make_admin('TEST Admin B', 'TEST-admin-b')
        cls.admin_a = make_admin('TEST Admin A', 'TEST-admin-a')
        cls.gone = make_admin('TEST Former Admin', 'TEST-gone')
        cls.gone.is_active = False
        cls.gone.save()
        get_user_model().objects.create_user('TEST Stray', password='TEST-stray')  # no group: not a login
        Cashier.objects.create(name='TEST Cashier', pin_hash=make_password('0123'))

    def test_dropdown_lists_admins_then_register_only(self):
        self.assertEqual(login_choices(), [('TEST Admin A', 'TEST Admin A'), ('TEST Admin B', 'TEST Admin B'),
                                           ('register', 'register')])
        page = self.client.get('/login/')
        self.assertContains(page, '<select name="username"')
        self.assertContains(page, '<option value="register">register</option>')
        self.assertContains(page, '<option value="TEST Admin A">TEST Admin A</option>')
        self.assertNotContains(page, 'TEST Cashier')
        self.assertNotContains(page, 'TEST Former Admin')
        self.assertNotContains(page, 'TEST Stray')
        self.assertNotContains(page, 'type="text"')

    def test_only_listed_logins_can_sign_in_even_when_posted_directly(self):
        # A PIN user is not a login at all; a stray/unlisted account is refused before any password check.
        for username, password in [('TEST Cashier', '0123'), ('TEST Stray', 'TEST-stray'), ('TEST Former Admin', 'TEST-gone')]:
            response = self.client.post('/login/', {'username': username, 'password': password})
            self.assertEqual(response.status_code, 200, username)
            self.assertContains(response, 'Choose your name from the list.')
            self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(LoginWindow.objects.exists())  # refused names never touch the throttle
        self.assertEqual(self.client.post('/login/', {'username': 'TEST Admin A', 'password': 'TEST-admin-a'}).status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)


class LoaderTests(TestCase):
    """The sample mirrors the shape of a user file with TEST values only."""
    sample = ('\n\nTEST app users and passwords\n\n\nAdmins : gets marked as logged in at register without pin\n'
              'TEST Admin password TEST-pass:one\nTEST Second Admin password TEST-pass-two\n\n'
              'Shared register User\nregister password TEST-register-pw\n\n'
              'Sub Accounts, Must be logged in as register user register\n'
              'TEST Cashier Pin 0123\nTEST Other Cashier Pin 9876\n\n'
              'Admins and sales register user gets drop down user menu at login screen.\n\n'
              'Subaccounts with pin get drop down menu of users at sub account login Sales, Sales Orders etc but do not get direct login at the primaryt login screen.\n')

    def load(self, text=None, *flags):
        out = StringIO()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'TEST-users.txt'
            path.write_text(self.sample if text is None else text)
            call_command('load_app_users', str(path), *flags, stdout=out)
        return out.getvalue()

    def test_loader_idempotent_and_only_hashes_stored(self):
        self.assertEqual(self.load(), '2 admins, 1 register login, 2 PIN users loaded; register login created from the file\n')
        User = get_user_model()
        admin = User.objects.get(username='TEST Admin')
        before = admin.password
        self.assertEqual(self.load(), '2 admins, 1 register login, 2 PIN users loaded; register login password: same as file\n')
        admin.refresh_from_db()
        self.assertEqual(admin.password, before)
        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(Cashier.objects.count(), 2)
        self.assertTrue(admin.check_password('TEST-pass:one'))
        self.assertNotEqual(admin.password, 'TEST-pass:one')
        self.assertTrue(admin.groups.filter(name=ADMIN_GROUP).exists())
        self.assertFalse(admin.is_superuser)
        register = User.objects.get(username='register')
        self.assertTrue(register.check_password('TEST-register-pw'))
        self.assertTrue(register.groups.filter(name=REGISTER_GROUP).exists())
        self.assertFalse(register.groups.filter(name=ADMIN_GROUP).exists())
        cashier = Cashier.objects.get(name='TEST Cashier')
        self.assertTrue(check_password('0123', cashier.pin_hash))
        self.assertNotEqual(cashier.pin_hash, '0123')
        self.assertFalse(User.objects.filter(username='TEST Cashier').exists())
        self.load(self.sample.replace('Pin 0123', 'Pin 4567'))
        cashier.refresh_from_db()
        self.assertTrue(check_password('4567', cashier.pin_hash))
        self.assertEqual(login_choices(), [('TEST Admin', 'TEST Admin'), ('TEST Second Admin', 'TEST Second Admin'),
                                           ('register', 'register')])

    def test_case_insensitive_reload_keeps_rows(self):
        self.load()
        self.load(self.sample.replace('TEST Cashier Pin', 'test cashier Pin').replace('TEST Admin password', 'test admin password'))
        self.assertEqual(Cashier.objects.count(), 2)
        self.assertEqual(get_user_model().objects.count(), 3)
        self.assertTrue(check_password('0123', Cashier.objects.get(name='TEST Cashier').pin_hash))

    def test_existing_register_password_kept_unless_flag(self):
        """the live site's shared login IS the register login; never change its password silently."""
        existing = make_register('register', 'TEST-live-password', is_staff=True)
        out = self.load()
        self.assertIn('register login password: differs from file (kept; use --set-register-password to change it)', out)
        self.assertNotIn('TEST-register-pw', out)
        existing.refresh_from_db()
        self.assertTrue(existing.check_password('TEST-live-password'))
        self.assertTrue(existing.is_active and existing.is_staff)
        self.assertTrue(existing.groups.filter(name=REGISTER_GROUP).exists())
        out = self.load(None, '--set-register-password')
        self.assertIn('register login password: changed to the file', out)
        existing.refresh_from_db()
        self.assertTrue(existing.check_password('TEST-register-pw'))
        self.assertIn('register login password: same as file', self.load())
        # An existing shared login without a group (the live site before this release) is adopted, not refused.
        existing.groups.clear()
        self.assertIn('differs from file', self.load(self.sample.replace('TEST-register-pw', 'TEST-newer-pw')))
        self.assertTrue(existing.groups.filter(name=REGISTER_GROUP).exists())

    def test_reload_deactivates_missing_and_reactivates_returning(self):
        """dropped users lose access but are never deleted; history keeps their names."""
        from sales.models import Document
        self.load()
        User = get_user_model()
        doc = Document.objects.create(kind='sale', status='completed', cashier='TEST Other Cashier')
        shorter = self.sample.replace('TEST Second Admin password TEST-pass-two\n', '').replace('TEST Other Cashier Pin 9876\n', '')
        out = self.load(shorter)
        self.assertEqual(out, '1 admins, 1 register login, 1 PIN users loaded; 1 admin and 1 PIN users deactivated; '
                              'register login password: same as file\n')
        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(Cashier.objects.count(), 2)
        second = User.objects.get(username='TEST Second Admin')
        self.assertFalse(second.is_active)
        other = Cashier.objects.get(name='TEST Other Cashier')
        self.assertFalse(other.active)
        doc.refresh_from_db()
        self.assertEqual(doc.cashier, 'TEST Other Cashier')
        self.assertEqual(login_choices(), [('TEST Admin', 'TEST Admin'), ('register', 'register')])
        self.assertEqual(list(Cashier.objects.filter(active=True).values_list('name', flat=True)), ['TEST Cashier'])
        # Second reload with the same shorter file: nothing new to report.
        self.assertEqual(self.load(shorter), '1 admins, 1 register login, 1 PIN users loaded; register login password: same as file\n')
        # Back in the file: reactivated with the same rows (names, history links intact).
        self.load()
        second.refresh_from_db(); other.refresh_from_db()
        self.assertTrue(second.is_active and other.active)
        self.assertTrue(second.check_password('TEST-pass-two'))
        self.assertEqual(Cashier.objects.count(), 2)
        self.assertEqual(User.objects.count(), 3)

    def test_malformed_and_duplicate_input_safe_and_atomic(self):
        for text in [self.sample.replace('Pin 0123', 'Pin sensitive-invalid'),
                     self.sample.replace('Pin 9876', 'Pin 0123'),
                     self.sample.replace('TEST Other Cashier Pin 9876', 'TEST Cashier Pin 9876'),
                     self.sample.replace('TEST Other Cashier Pin 9876', 'register Pin 9876'),
                     self.sample.replace('TEST Cashier Pin 0123', 'TEST Cashier Pn 0123'),
                     self.sample.replace('TEST Cashier Pin 0123', 'TEST Cashier password 0123'),
                     self.sample.replace('Sub Accounts, Must be logged in as register user register\n', ''),
                     self.sample.replace('TEST Admin password TEST-pass:one\n', 'TEST Admin password TEST-pass:one\nTEST Admin password TEST-dup\n')]:
            with self.assertRaises(CommandError) as raised:
                self.load(text)
            for secret in ['sensitive-invalid', 'TEST-pass', 'TEST-register', '0123', '9876']:
                self.assertNotIn(secret, str(raised.exception))
            self.assertFalse(Cashier.objects.exists())
            self.assertFalse(get_user_model().objects.exists())

    def test_same_name_may_be_admin_and_pin_user(self):
        """one person as an admin login AND a PIN user. Loads as one login + one Cashier row, and
        both ways of ringing stamp the same cashier name, so reports count one person."""
        text = self.sample.replace('TEST Other Cashier Pin 9876', 'TEST Admin Pin 9876')
        self.assertEqual(self.load(text), '2 admins, 1 register login, 2 PIN users loaded; register login created from the file\n')
        User = get_user_model()
        admin = User.objects.get(username='TEST Admin')
        self.assertTrue(admin.groups.filter(name='Admin').exists())
        self.assertEqual(Cashier.objects.filter(name='TEST Admin', active=True).count(), 1)
        self.assertEqual(self.load(text), '2 admins, 1 register login, 2 PIN users loaded; register login password: same as file\n')
        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(Cashier.objects.count(), 2)
        # Signed in as the admin: cashier = her login name.
        with override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True):
            self.check_both_ways_stamp_one_name(admin)

    def check_both_ways_stamp_one_name(self, admin):
        User = get_user_model()
        self.client.force_login(admin)
        self.assertContains(self.client.get('/'), 'Cashier: <span id="cashier-name">TEST Admin</span>')
        self.client.logout()
        # Under the register login, switched in by PIN: the same name, from the Cashier row.
        self.client.force_login(User.objects.get(username='register'))
        self.assertContains(self.client.get('/'), 'TEST Admin')  # offered in the PIN pop-up
        pk = Cashier.objects.get(name='TEST Admin').pk
        self.assertEqual(self.client.post('/cashier/', {'cashier': pk, 'pin': '9876'}).status_code, 200)
        self.assertContains(self.client.get('/'), 'Cashier: <span id="cashier-name">TEST Admin</span>')

    def test_same_name_still_refused_where_it_must_be(self):
        text = self.sample.replace('TEST Other Cashier Pin 9876', 'TEST Admin Pin 9876')
        # Still refused: the same name twice in one section, and the register login as an admin or a PIN user.
        for bad in [text.replace('TEST Second Admin password TEST-pass-two', 'TEST Admin password TEST-pass-two'),
                    text.replace('TEST Cashier Pin 0123', 'TEST Admin Pin 0123'),
                    text.replace('TEST Cashier Pin 0123', 'register Pin 0123'),
                    text.replace('TEST Second Admin password TEST-pass-two', 'register password TEST-pass-two')]:
            with self.assertRaises(CommandError) as raised:
                self.load(bad)
            self.assertIn('Duplicate', str(raised.exception))

    def test_existing_unrelated_login_not_promoted(self):
        user = get_user_model().objects.create_user('TEST Admin', password='TEST-existing')
        with self.assertRaises(CommandError):
            self.load()
        user.refresh_from_db()
        self.assertTrue(user.check_password('TEST-existing'))
        self.assertFalse(user.groups.exists())
        self.assertFalse(Cashier.objects.exists())
        # An admin cannot be listed as the register login either.
        user.delete()
        make_admin('TEST Register Admin', 'x')
        with self.assertRaises(CommandError):
            self.load(self.sample.replace('register password', 'TEST Register Admin password'))


@override_settings(MIDDLEWARE=MIDDLEWARE, POS_REQUIRE_LOGIN=True)
class CashierTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_register(is_staff=True)
        cls.admin = make_admin()
        cls.cashier = Cashier.objects.create(name='TEST Cashier', pin_hash=make_password('0123'))
        cls.other = Cashier.objects.create(name='TEST Other', pin_hash=make_password('0123'))  # same PIN on purpose

    def pin(self, pin, cashier=None):
        return self.client.post('/cashier/', {'pin': pin, 'cashier': (cashier or self.cashier).pk})

    def test_admin_login_always_admin_and_is_staff_register_login_is_not(self):
        self.assertEqual(self.client.post('/login/', {'username': 'TEST Admin', 'password': 'TEST-admin'}).status_code, 302)
        self.assertContains(self.client.get('/'), 'class="admin-mode-badge')
        self.assertEqual(self.client.get('/reports/').status_code, 200)
        self.client.post('/logout/')
        self.client.post('/login/', {'username': 'register', 'password': 'TEST-shared'})
        self.assertTrue(self.user.is_staff)
        self.assertNotContains(self.client.get('/'), 'class="admin-mode-badge')
        # no File-menu switch any more; the register login has no way into Admin Mode.
        self.assertEqual(self.client.post('/admin-mode/', HTTP_ACCEPT='application/json').status_code, 404)
        self.assertRedirects(self.client.get('/reports/'), '/?admin=off', fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/server-status/'), '/?admin=off', fetch_redirect_response=False)

    def test_admin_is_the_cashier_automatically_without_pin_switching(self):
        """signed-in admin = active cashier; the pop-up offers no PIN switching to admins."""
        self.client.force_login(self.admin)
        page = self.client.get('/')
        self.assertContains(page, 'Cashier: <span id="cashier-name">TEST Admin</span>')
        self.assertNotContains(page, 'id="cashier-control"')
        self.assertNotContains(page, 'id="cashier-dialog"')
        self.assertNotContains(page, 'TEST Cashier')
        self.assertEqual(self.pin('0123').status_code, 403)
        self.assertEqual(self.client.post('/cashier/', {'action': 'clear'}).status_code, 403)
        self.assertNotIn(cashiers.SESSION_KEY, self.client.session)

    def test_pin_cannot_login_even_when_login_gate_off(self):
        with override_settings(POS_REQUIRE_LOGIN=False):
            self.assertEqual(self.pin('0123').status_code, 401)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotIn(cashiers.SESSION_KEY, self.client.session)

    def test_pin_popup_only_for_register_login(self):
        """PIN users only under the register login; a stray login gets no pop-up and no switching."""
        stray = get_user_model().objects.create_user('TEST Stray')
        self.client.force_login(stray)
        page = self.client.get('/')
        self.assertNotContains(page, 'id="cashier-control"')
        self.assertNotContains(page, 'cashier-name')
        self.assertEqual(self.pin('0123').status_code, 403)
        self.client.force_login(self.user)
        page = self.client.get('/')
        self.assertContains(page, 'id="cashier-control"')
        self.assertContains(page, '<select id="cashier-user" name="cashier"')
        self.assertContains(page, f'<option value="{self.cashier.pk}">TEST Cashier</option>')
        self.assertContains(page, f'<option value="{self.other.pk}">TEST Other</option>')

    def test_pin_checked_against_chosen_cashier_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/cashier/').status_code, 405)
        self.assertEqual(self.pin('0123', self.cashier).json(), {'cashier': 'TEST Cashier'})
        self.assertEqual(self.client.session[cashiers.SESSION_KEY], self.cashier.pk)
        self.assertContains(self.client.get('/'), f'<option value="{self.cashier.pk}" selected>TEST Cashier</option>')
        # Same PIN, other name: it is the chosen person who is activated, never "whoever has this PIN".
        self.assertEqual(self.pin('0123', self.other).json(), {'cashier': 'TEST Other'})
        self.assertEqual(self.client.session[cashiers.SESSION_KEY], self.other.pk)
        self.assertEqual(self.pin('9999').json(), {'error': 'PIN not recognized'})
        self.assertEqual(self.client.session[cashiers.SESSION_KEY], self.other.pk)
        self.assertEqual(self.client.post('/cashier/', {'pin': '0123'}).json(), {'error': 'Choose your name from the list.'})
        self.assertEqual(self.client.post('/cashier/', {'pin': '0123', 'cashier': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/cashier/', {'pin': '0123', 'cashier': 999999}).status_code, 400)
        self.client.post('/cashier/', {'action': 'clear'})
        self.assertNotIn(cashiers.SESSION_KEY, self.client.session)
        self.assertContains(self.client.get('/'), 'No cashier')
        self.pin('0123')
        self.cashier.active = False
        self.cashier.save()
        self.assertContains(self.client.get('/'), 'No cashier')
        self.assertNotContains(self.client.get('/'), f'<option value="{self.cashier.pk}"')
        self.assertEqual(self.pin('0123').status_code, 400)  # inactive = not selectable
        self.cashier.active = True
        self.cashier.save()
        self.client.post('/logout/')
        self.assertNotIn(cashiers.SESSION_KEY, self.client.session)

    @override_settings(CASHIER_TIMEOUT_MINUTES=1)
    def test_cashier_inactivity_does_not_log_out(self):
        self.client.force_login(self.user)
        self.pin('0123')
        session = self.client.session
        session[cashiers.LAST_SEEN] = timezone.now().timestamp() - 61
        session.save()
        self.assertContains(self.client.get('/'), 'No cashier')
        self.assertIn('_auth_user_id', self.client.session)
        self.assertNotIn(cashiers.SESSION_KEY, self.client.session)

    def test_cashier_control_on_shared_page_families(self):
        self.client.force_login(self.user)
        self.pin('0123')
        for path in ['/', '/customers/', '/purchasing/items/', '/pos/history/',
                     '/pos/orders/', '/pos/sale/new/', '/pos/order/new/', '/fixes/', '/tags/']:
            response = self.client.get(path)
            self.assertContains(response, 'id="cashier-control"', msg_prefix=path)
            self.assertContains(response, 'id="cashier-user"', msg_prefix=path)
            self.assertContains(response, 'Cashier: <span id="cashier-name">TEST Cashier</span>', msg_prefix=path)
        self.client.force_login(self.admin)
        for path in ['/', '/pos/sale/new/', '/pos/order/new/']:
            self.assertContains(self.client.get(path), 'Cashier: <span id="cashier-name">TEST Admin</span>', msg_prefix=path)

    def test_ringing_name_in_register_title_bar(self):
        # the green title bar on register screens names who is ringing (PIN user or signed-in admin);
        # with nobody active it is a button that opens the Cashier window.
        self.client.force_login(self.user)
        for path in ['/pos/sale/new/', '/pos/order/new/', '/pos/history/']:
            page = self.client.get(path)
            self.assertContains(page, '<button type="button" class="ringing ringing-none" id="ringing-name"', msg_prefix=path)
            self.assertContains(page, 'No cashier — click to choose', msg_prefix=path)
        page = self.client.get('/pos/sale/new/')
        self.assertContains(page, 'id="cashier-clear"')
        self.assertContains(page, ' hidden>Sign out <span id="cashier-clear-name"></span>')  # nobody active → no sign-out button
        self.assertNotContains(page, 'id="ringing-signout"')
        self.pin('0123')
        page = self.client.get('/pos/sale/new/')
        self.assertContains(page, '<button type="button" class="ringing ringing-active" id="ringing-name" title="Click to switch who is ringing" aria-haspopup="dialog">TEST Cashier</button>')
        self.assertNotContains(page, 'class="ringing ringing-none"')
        self.assertContains(page, '>Sign in</button>')  # "Activate" → "Sign in"
        self.assertNotContains(page, '>Activate<')
        self.assertContains(page, '<button type="button" class="ringing-signout" id="ringing-signout" title="Sign out this cashier">Sign out</button>')
        self.assertContains(page, 'Sign out <span id="cashier-clear-name">TEST Cashier</span>')
        self.assertNotContains(page, '>Clear<')
        self.client.force_login(self.admin)
        page = self.client.get('/pos/order/new/')
        self.assertContains(page, '<span class="ringing" id="ringing-name" title="Who is ringing this up">TEST Admin</span>')
        self.assertNotContains(page, 'id="ringing-signout"')  # admins are the cashier automatically; no PIN sign-out
        from sales.models import Document
        doc = Document.objects.create(kind='order', status='open')
        page = self.client.get(f'/pos/order/{doc.token}/')
        self.assertContains(page, 'id="ringing-name"')
        self.assertContains(page, '<span class="doc-status">Open</span>')  # the status badge stays, right of the name
        self.client.logout()
        self.assertNotContains(self.client.get('/pos/sale/new/', follow=True), 'ringing-name')

    def test_per_user_lockout_and_per_register_guard(self):
        self.client.force_login(self.user)
        for _ in range(10):
            self.assertEqual(self.pin('9999', self.cashier).status_code, 400)
        locked = self.pin('0123', self.cashier)
        self.assertEqual(locked.status_code, 429)
        self.assertIn('for TEST Cashier', locked.json()['error'])
        # Only that person is locked; another name still works at this register.
        self.assertEqual(self.pin('0123', self.other).status_code, 200)
        self.assertEqual(LoginWindow.objects.get(key=f'pin:{self.cashier.pk}').failures, 10)
        LoginWindow.objects.filter(key=f'pin:{self.cashier.pk}').update(started_at=timezone.now()-timedelta(minutes=16))
        self.assertEqual(self.pin('0123', self.cashier).status_code, 200)
        # Register guard: 20 wrong PINs from one signed-in session, spread over names, lock the pop-up there.
        LoginWindow.objects.all().delete()
        for n in range(20):
            self.assertEqual(self.pin('9999', self.cashier if n % 2 else self.other).status_code, 400)
            LoginWindow.objects.filter(key__startswith='pin:').update(failures=0)  # keep the per-user limit out of it
        locked = self.pin('0123', self.cashier)
        self.assertEqual(locked.status_code, 429)
        self.assertIn('at this register', locked.json()['error'])
        # A different signed-in session (another register) is not affected.
        other_register = Client()
        other_register.force_login(self.user)
        self.assertEqual(other_register.post('/cashier/', {'pin': '0123', 'cashier': self.cashier.pk}).status_code, 200)
        LoginWindow.objects.filter(key__startswith='pin-register:').update(started_at=timezone.now()-timedelta(minutes=16))
        self.assertEqual(self.pin('0123', self.cashier).status_code, 200)
        # A success resets both counters.
        self.assertEqual(LoginWindow.objects.filter(key__startswith='pin-register:', failures=0).count(), 2)
        self.assertTrue(all(len(w.key) <= 64 for w in LoginWindow.objects.all()))  # column limit (Postgres enforces it)

    def test_pin_csrf_protected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/cashier/', {'pin': '0123', 'cashier': self.cashier.pk}).status_code, 403)


@override_settings(MIDDLEWARE=MIDDLEWARE,POS_REQUIRE_LOGIN=True)
class TestingBannerRemovedTests(TestCase):
    """the yellow TESTING ENVIRONMENT banner is gone from every page (login, home, register, customers, items)."""

    def test_no_testing_banner_anywhere(self):
        admin = make_admin()
        self.assertNotContains(self.client.get('/login/'), 'TESTING ENVIRONMENT')
        self.client.force_login(admin)
        for path in ['/', '/pos/sale/new/', '/pos/order/new/', '/customers/', '/purchasing/items/', '/fixes/']:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertNotContains(page, 'testing-banner', msg_prefix=path)
            self.assertNotContains(page, 'TESTING ENVIRONMENT', msg_prefix=path)
