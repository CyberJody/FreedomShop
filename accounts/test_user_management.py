# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise public HTTP actions with forged fields, CSRF checks and transactional audit failures."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import Group
from django.core.cache import caches
from django.db import close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from sales.models import Document, OrderPayment, OrderPaymentChange
from shop_settings.test_setup import CapturedLogs
from .models import AuditEntry, Cashier
from .roles import ADMIN_GROUP, REGISTER_GROUP
from .signin_helpers import AUTH_MIDDLEWARE, make_test_user
from .user_management import REAUTH_FAILURE, REAUTH_LOCKED

PASSWORD = 'Cedar-Harbor-9147-TEST'
NEW_PASSWORD = 'Maple-Lantern-8256-TEST'
FULL_MIDDLEWARE = AUTH_MIDDLEWARE + ['django.middleware.csrf.CsrfViewMiddleware']
VALIDATORS = [{'NAME': f'django.contrib.auth.password_validation.{name}'} for name in (
    'UserAttributeSimilarityValidator', 'MinimumLengthValidator', 'CommonPasswordValidator', 'NumericPasswordValidator')]
TEST_SETTINGS = dict(MIDDLEWARE=FULL_MIDDLEWARE, AUTH_PASSWORD_VALIDATORS=VALIDATORS,
                     PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


@override_settings(**TEST_SETTINGS)
class UserManagementTests(TestCase):
    def setUp(self):
        self.admin = make_test_user('demo.admin', ADMIN_GROUP, PASSWORD)
        self.other = make_test_user('second.admin', ADMIN_GROUP, PASSWORD)
        self.register = make_test_user('demo.register', REGISTER_GROUP, PASSWORD)
        self.cashier = Cashier.objects.create(name='Demo Cashier', pin_hash=make_password('0567'))
        self.client.force_login(self.admin)
        caches['admin_reauthentication'].clear()

    def login_url(self, action, user=None):
        if action == 'add':
            return reverse('users-login-add')
        return reverse('users-login-change', args=[(user or self.register).pk, action])

    def cashier_url(self, action, cashier=None):
        if action == 'add':
            return reverse('users-cashier-add')
        return reverse('users-cashier-change', args=[(cashier or self.cashier).pk, action])

    def post_login(self, action, user=None, **data):
        return self.client.post(self.login_url(action, user), data, follow=True)

    def post_cashier(self, action, cashier=None, **data):
        return self.client.post(self.cashier_url(action, cashier), data, follow=True)

    def assert_audit(self, action):
        entry = AuditEntry.objects.first()
        self.assertEqual(entry.action, 'users.' + action)
        self.assertEqual(entry.who, self.admin)
        return entry

    def all_urls(self):
        return ([reverse('users-settings'), self.login_url('add'), self.cashier_url('add')]
                + [self.login_url(action) for action in ('rename', 'disable', 'enable', 'password', 'role', 'delete')]
                + [self.cashier_url(action) for action in ('rename', 'pin', 'remove', 'disable', 'enable')])

    def test_anonymous_redirected_on_every_view_and_method(self):
        self.client.logout()
        for url in self.all_urls():
            for method in ('get', 'post'):
                with self.subTest(url=url, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith('/login/?next='))
        self.assertFalse(AuditEntry.objects.exists())

    def test_register_forbidden_on_every_view_and_method_even_when_staff(self):
        self.register.is_staff = self.register.is_superuser = True
        self.register.save()
        self.client.force_login(self.register)
        for url in self.all_urls():
            for method in ('get', 'post'):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url).status_code, 403)
        self.assertFalse(AuditEntry.objects.exists())

    def test_unassigned_superuser_is_not_an_admin(self):
        user = get_user_model().objects.create_user('unassigned', password=PASSWORD, is_superuser=True, is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('users-settings')).status_code, 403)

    def test_admin_can_open_every_form_without_mutation(self):
        initial = (get_user_model().objects.count(), Cashier.objects.count())
        for url in self.all_urls():
            with self.subTest(url=url):
                response = self.client.get(url, {'action': 'delete', 'role': 'register'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('no-store', response['Cache-Control'])
                self.assertEqual(self.client.head(url).status_code, 200)
        self.assertFalse(AuditEntry.objects.exists())
        self.assertEqual(initial, (get_user_model().objects.count(), Cashier.objects.count()))

    def test_get_only_list_and_unknown_actions(self):
        self.assertEqual(self.client.post(reverse('users-settings')).status_code, 405)
        for url in (self.login_url('unknown'), self.cashier_url('unknown')):
            self.assertEqual(self.client.post(url).status_code, 404)
        self.assertEqual(self.client.post(f'/settings/users/logins/{self.other.pk}/add/').status_code, 404)
        self.assertEqual(self.client.delete(self.login_url('disable')).status_code, 405)

    def test_navigation_and_business_link_only_for_admin(self):
        self.assertContains(self.client.get('/'), 'href="/settings/users/"')
        self.assertContains(self.client.get('/settings/business/'), 'href="/settings/users/"')
        self.assertContains(self.client.get('/settings/business/'), 'Run setup again')
        self.client.force_login(self.register)
        self.assertNotContains(self.client.get('/'), 'href="/settings/users/"')

    def test_table_shows_logins_including_inactive_and_last_login(self):
        self.register.is_active = False
        self.register.save()
        response = self.client.get(reverse('users-settings'))
        for text in ('demo.register', 'second.admin', 'Last login', 'Register', 'Admin', 'Demo Cashier'):
            self.assertContains(response, text)

    def test_add_register_and_add_admin(self):
        for role, username in ((REGISTER_GROUP, 'new.register'), (ADMIN_GROUP, 'new.admin')):
            with self.subTest(role=role):
                response = self.post_login('add', username=username, role=role,
                                           password1=NEW_PASSWORD, password2=NEW_PASSWORD, current_password=PASSWORD)
                self.assertContains(response, 'Add login completed.')
                user = get_user_model().objects.get(username=username)
                self.assertTrue(user.check_password(NEW_PASSWORD))
                self.assertEqual(list(user.groups.values_list('name', flat=True)), [role])
                self.assertFalse(user.is_superuser)
                self.assertFalse(user.is_staff)
                self.assert_audit('login.add')

    def test_add_register_does_not_require_current_password(self):
        self.post_login('add', username='extra.register', role=REGISTER_GROUP,
                        password1=NEW_PASSWORD, password2=NEW_PASSWORD)
        self.assertTrue(get_user_model().objects.filter(username='extra.register').exists())

    def test_cannot_inject_privileged_fields(self):
        self.post_login('add', username='extra.register', role=REGISTER_GROUP, password1=NEW_PASSWORD,
                        password2=NEW_PASSWORD, is_superuser='on', is_staff='on', groups=ADMIN_GROUP)
        user = get_user_model().objects.get(username='extra.register')
        self.assertFalse(user.is_superuser or user.is_staff or user.groups.filter(name=ADMIN_GROUP).exists())

    def test_username_validation_and_case_insensitive_uniqueness(self):
        for username in ('bad name', '<script>alert(1)</script>', 'demo.ADMIN', 'a' * 151):
            with self.subTest(username=username):
                self.post_login('add', username=username, role=REGISTER_GROUP,
                                password1=NEW_PASSWORD, password2=NEW_PASSWORD)
        self.assertEqual(get_user_model().objects.count(), 3)
        self.assertFalse(AuditEntry.objects.exists())

    def test_password_validation_on_add_and_reset(self):
        for password in ('123', 'password', '482659172634', 'demo.register'):
            with self.subTest(password=password):
                self.post_login('add', username='demo.register2', role=REGISTER_GROUP,
                                password1=password, password2=password)
                self.post_login('password', new_password1=password, new_password2=password)
                self.assertTrue(get_user_model().objects.get(pk=self.register.pk).check_password(PASSWORD))
        self.assertFalse(AuditEntry.objects.exists())

    def test_password_confirmation_mismatch_is_refused(self):
        self.post_login('add', username='new.register', role=REGISTER_GROUP,
                        password1=NEW_PASSWORD, password2=PASSWORD)
        self.post_login('password', new_password1=NEW_PASSWORD, new_password2=PASSWORD)
        self.assertFalse(AuditEntry.objects.exists())

    def test_rename_register_and_duplicate_refused(self):
        self.post_login('rename', username='renamed.register')
        self.register.refresh_from_db()
        self.assertEqual(self.register.username, 'renamed.register')
        entry = self.assert_audit('login.rename')
        self.assertEqual(entry.details['before']['username'], 'demo.register')
        self.assertEqual(entry.details['after']['username'], 'renamed.register')
        self.post_login('rename', username='DEMO.ADMIN')
        self.register.refresh_from_db()
        self.assertEqual(self.register.username, 'renamed.register')
        self.assertEqual(AuditEntry.objects.count(), 1)

    def test_enable_disable_register(self):
        for action, active in (('disable', False), ('enable', True)):
            self.post_login(action)
            self.register.refresh_from_db()
            self.assertEqual(self.register.is_active, active)
            self.assert_audit('login.' + action)

    def test_reset_register_password(self):
        self.post_login('password', new_password1=NEW_PASSWORD, new_password2=NEW_PASSWORD)
        self.register.refresh_from_db()
        self.assertTrue(self.register.check_password(NEW_PASSWORD))
        self.assert_audit('login.password')

    def test_promote_and_demote_another_login(self):
        self.post_login('role', role=ADMIN_GROUP, current_password=PASSWORD)
        self.assertTrue(self.register.groups.filter(name=ADMIN_GROUP).exists())
        self.assertFalse(self.register.groups.filter(name=REGISTER_GROUP).exists())
        self.post_login('role', role=REGISTER_GROUP, current_password=PASSWORD)
        self.assertFalse(self.register.groups.filter(name=ADMIN_GROUP).exists())
        self.assertTrue(self.register.groups.filter(name=REGISTER_GROUP).exists())
        self.assertEqual(AuditEntry.objects.count(), 2)

    def test_delete_register_and_another_admin(self):
        for target in (self.register, self.other):
            self.post_login('delete', target, current_password=PASSWORD)
            self.assertFalse(get_user_model().objects.filter(pk=target.pk).exists())
            self.assertIsNone(self.assert_audit('login.delete').details['after'])

    def test_last_active_admin_disable_demote_delete_refused(self):
        self.other.is_active = False
        self.other.save()
        for action in ('disable', 'role', 'delete'):
            with self.subTest(action=action):
                response = self.post_login(action, self.admin, role=REGISTER_GROUP, current_password=PASSWORD)
                self.assertContains(response, 'last active admin')
                self.admin.refresh_from_db()
                self.assertTrue(self.admin.is_active)
                self.assertTrue(self.admin.groups.filter(name=ADMIN_GROUP).exists())
        self.assertFalse(AuditEntry.objects.exists())

    def test_self_disable_demote_delete_refused_with_another_active_admin(self):
        for action in ('disable', 'role', 'delete'):
            response = self.post_login(action, self.admin, role=REGISTER_GROUP, current_password=PASSWORD)
            self.assertContains(response, 'your own')
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertTrue(self.admin.groups.filter(name=ADMIN_GROUP).exists())
        self.assertFalse(AuditEntry.objects.exists())

    def test_inactive_admin_does_not_satisfy_last_admin_guard(self):
        self.other.is_active = False
        self.other.save()
        self.post_login('role', self.admin, role=REGISTER_GROUP, current_password=PASSWORD)
        self.assertTrue(self.admin.groups.filter(name=ADMIN_GROUP).exists())

    def test_every_admin_change_requires_password(self):
        for action in ('rename', 'disable', 'enable', 'password', 'role', 'delete'):
            data = dict(username='other.renamed', role=REGISTER_GROUP,
                        new_password1=NEW_PASSWORD, new_password2=NEW_PASSWORD)
            with self.subTest(action=action):
                self.post_login(action, self.other, **data)
        self.post_login('add', username='new.admin', role=ADMIN_GROUP,
                        password1=NEW_PASSWORD, password2=NEW_PASSWORD)
        self.post_login('role', role=ADMIN_GROUP)
        self.assertFalse(AuditEntry.objects.exists())
        self.other.refresh_from_db()
        self.assertEqual(self.other.username, 'second.admin')
        self.assertTrue(self.other.is_active)
        self.assertTrue(self.other.check_password(PASSWORD))

    def test_wrong_password_is_generic_and_authenticates_actor(self):
        with mock.patch('accounts.user_management.authenticate', return_value=None) as auth:
            response = self.post_login('rename', self.other, username='other.renamed', current_password='wrong')
        self.assertContains(response, REAUTH_FAILURE)
        self.assertEqual(auth.call_args.kwargs['username'], self.admin.username)
        self.assertFalse(AuditEntry.objects.exists())

    def test_target_password_cannot_confirm_actor_identity(self):
        self.other.set_password(NEW_PASSWORD)
        self.other.save()
        response = self.post_login('rename', self.other, username='other.renamed', current_password=NEW_PASSWORD)
        self.assertContains(response, REAUTH_FAILURE)
        self.assertFalse(AuditEntry.objects.exists())

    def test_valid_reauthentication_for_each_admin_change(self):
        for action, data in (('rename', {'username': 'other.renamed'}), ('disable', {}), ('enable', {}),
                             ('password', {'new_password1': NEW_PASSWORD, 'new_password2': NEW_PASSWORD}),
                             ('role', {'role': REGISTER_GROUP})):
            self.post_login(action, self.other, current_password=PASSWORD, **data)
            self.assert_audit('login.' + action)
        self.assertEqual(AuditEntry.objects.count(), 5)

    def test_five_failures_block_correct_password_and_other_target(self):
        for number in range(5):
            response = self.post_login('rename', self.other, username='other.renamed', current_password='wrong')
            self.assertContains(response, REAUTH_FAILURE)
        with mock.patch('accounts.user_management.authenticate') as auth:
            response = self.post_login('role', role=ADMIN_GROUP, current_password=PASSWORD)
            self.assertContains(response, REAUTH_LOCKED)
            auth.assert_not_called()
        self.assertFalse(AuditEntry.objects.exists())

    def test_reauthentication_limit_is_per_actor_and_expires(self):
        started = timezone.now()
        with mock.patch('accounts.user_management.timezone.now', return_value=started):
            for number in range(5):
                self.post_login('rename', self.other, username='other.renamed', current_password='wrong')
        other_client = Client()
        other_client.force_login(self.other)
        response = other_client.post(self.login_url('rename', self.admin),
                                     {'username': 'demo.renamed', 'current_password': PASSWORD}, follow=True)
        self.assertContains(response, 'Rename login completed.')
        with mock.patch('accounts.user_management.timezone.now', return_value=started + timedelta(minutes=16)):
            self.post_login('rename', self.other, username='other.renamed', current_password=PASSWORD)
        self.other.refresh_from_db()
        self.assertEqual(self.other.username, 'other.renamed')

    def test_success_does_not_erase_prior_failures(self):
        for number in range(4):
            self.post_login('rename', self.other, username='other.renamed', current_password='wrong')
        self.post_login('rename', self.other, username='other.renamed', current_password=PASSWORD)
        self.post_login('disable', self.other, current_password='wrong')
        self.assertContains(self.post_login('disable', self.other, current_password=PASSWORD), REAUTH_LOCKED)
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_active)

    def test_window_starts_at_first_failure_and_does_not_extend(self):
        started = timezone.now()
        key = f'admin-reauth:{self.admin.pk}'
        with mock.patch('accounts.user_management.timezone.now', return_value=started):
            self.post_login('rename', self.other, username='other.renamed', current_password=PASSWORD)
        self.assertIsNone(caches['admin_reauthentication'].get(key))
        with mock.patch('accounts.user_management.timezone.now', return_value=started + timedelta(minutes=10)):
            self.post_login('disable', self.other, current_password='wrong')
        until = caches['admin_reauthentication'].get(key)['until']
        self.assertEqual(until, (started + timedelta(minutes=25)).timestamp())
        with mock.patch('accounts.user_management.timezone.now', return_value=started + timedelta(minutes=14)):
            self.post_login('disable', self.other, current_password='wrong')
        self.assertEqual(caches['admin_reauthentication'].get(key)['until'], until)

    def test_cache_write_failure_refuses_authentication(self):
        with mock.patch.object(caches['admin_reauthentication'], 'set', return_value=False), \
                mock.patch('accounts.user_management.authenticate') as auth:
            self.assertContains(self.post_login('disable', self.other, current_password=PASSWORD), REAUTH_FAILURE)
            auth.assert_not_called()
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_active)

    def test_cache_capacity_does_not_evict_unexpired_failure_counters(self):
        cache = caches['admin_reauthentication']
        with mock.patch.object(cache, '_max_entries', 1):
            cache.set('first-admin', 5, timeout=900)
            cache.set('second-admin', 4, timeout=900)
            cache.set('third-admin', 3, timeout=900)
        self.assertEqual(cache.get('first-admin'), 5)
        self.assertEqual(cache.get('second-admin'), 4)

    def test_cache_counter_survives_a_new_backend_instance(self):
        from django.conf import settings
        from .reauth_cache import ReauthenticationCache

        for number in range(5):
            self.post_login('disable', self.other, current_password='wrong')
        config = settings.CACHES['admin_reauthentication']
        independent = ReauthenticationCache(config['LOCATION'], config)
        self.assertEqual(independent.get(f'admin-reauth:{self.admin.pk}')['failures'], 5)

    def test_own_password_reset_requires_signing_in_again(self):
        response = self.client.post(self.login_url('password', self.admin), {
            'current_password': PASSWORD, 'new_password1': NEW_PASSWORD, 'new_password2': NEW_PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(NEW_PASSWORD))
        self.assert_audit('login.password')
        self.assertRedirects(self.client.get(reverse('users-settings')), '/login/?next=/settings/users/',
                             fetch_redirect_response=False)

    def test_forged_current_actor_and_target_ids_are_ignored(self):
        self.post_login('disable', user_id=self.admin.pk, target=self.admin.pk, actor=self.register.pk)
        self.register.refresh_from_db()
        self.admin.refresh_from_db()
        self.assertFalse(self.register.is_active)
        self.assertTrue(self.admin.is_active)
        self.assertEqual(self.assert_audit('login.disable').who, self.admin)

    def test_actor_permission_rechecked_after_taking_lock(self):
        from .user_views import locked_actor

        def demote_then_lock(request):
            request.user.groups.remove(Group.objects.get(name=ADMIN_GROUP))
            return locked_actor(request)

        with mock.patch('accounts.user_views.locked_actor', side_effect=demote_then_lock):
            self.assertEqual(self.client.post(self.login_url('disable')).status_code, 403)
        self.register.refresh_from_db()
        self.assertTrue(self.register.is_active)
        self.assertFalse(AuditEntry.objects.exists())

    def test_add_cashier_hashes_pin_and_requires_confirmation(self):
        response = self.post_cashier('add', name='New Cashier', pin='019283', pin_again='019283')
        self.assertContains(response, 'Add cashier completed.')
        cashier = Cashier.objects.get(name='New Cashier')
        self.assertNotEqual(cashier.pin_hash, '019283')
        self.assertTrue(check_password('019283', cashier.pin_hash))
        self.assert_audit('cashier.add')

    def test_cashier_pin_rejects_invalid_lengths_non_ascii_and_mismatch(self):
        for pin, confirmation in (('123', '123'), ('1234567', '1234567'), ('１２３４', '１２３４'),
                                  ('١٢٣٤', '١٢٣٤'), ('abcd', 'abcd'), ('1234 ', '1234 '), ('1234', '4321')):
            with self.subTest(pin=pin):
                self.post_cashier('add', name='New Cashier', pin=pin, pin_again=confirmation)
                self.post_cashier('pin', pin=pin, pin_again=confirmation)
        self.assertFalse(Cashier.objects.filter(name='New Cashier').exists())
        self.assertFalse(AuditEntry.objects.exists())
        self.cashier.refresh_from_db()
        self.assertTrue(check_password('0567', self.cashier.pin_hash))

    def test_cashier_name_required_length_and_duplicate_checked(self):
        for name in (' ', 'DEMO CASHIER', 'a' * 101):
            self.post_cashier('add', name=name, pin='1234', pin_again='1234')
        self.assertEqual(Cashier.objects.count(), 1)
        self.assertFalse(AuditEntry.objects.exists())

    def test_cashier_add_rejects_other_retained_cashiers_names(self):
        sale = Document.objects.create(kind='sale', status='completed', cashier=self.cashier.name)
        self.post_cashier('rename', name='Second Name')
        self.post_cashier('rename', name='Current Name')
        for active in (True, False):
            self.cashier.active = active
            self.cashier.save(update_fields=['active'])
            for name in ('demo cashier', 'DEMO CASHIER', 'sECOND nAME', 'CURRENT NAME'):
                with self.subTest(active=active, name=name), transaction.atomic():
                    audit_count = AuditEntry.objects.count()
                    response = self.post_cashier('add', name=name, pin='2468', pin_again='2468')
                    self.assertContains(response, 'A cashier with this name already exists.')
                    self.assertEqual(Cashier.objects.count(), 1)
                    self.assertEqual(AuditEntry.objects.count(), audit_count)
        sale.refresh_from_db()
        self.assertEqual(sale.cashier, 'Demo Cashier')
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.previous_names, ['Demo Cashier', 'Second Name'])

    def test_cashier_rename_rejects_other_retained_cashiers_names(self):
        sale = Document.objects.create(kind='sale', status='completed', cashier=self.cashier.name)
        self.post_cashier('rename', name='Second Name')
        self.post_cashier('rename', name='Current Name')
        other = Cashier.objects.create(name='Other Cashier', pin_hash=make_password('2468'))
        for active in (True, False):
            self.cashier.active = active
            self.cashier.save(update_fields=['active'])
            for name in ('demo cashier', 'DEMO CASHIER', 'sECOND nAME', 'CURRENT NAME'):
                with self.subTest(active=active, name=name), transaction.atomic():
                    audit_count = AuditEntry.objects.count()
                    response = self.post_cashier('rename', other, name=name)
                    self.assertContains(response, 'A cashier with this name already exists.')
                    other.refresh_from_db()
                    self.assertEqual(other.name, 'Other Cashier')
                    self.assertEqual(other.previous_names, [])
                    self.assertEqual(AuditEntry.objects.count(), audit_count)
        sale.refresh_from_db()
        self.assertEqual(sale.cashier, 'Demo Cashier')
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.previous_names, ['Demo Cashier', 'Second Name'])

    def test_cashier_can_rename_back_to_own_alias(self):
        sale = Document.objects.create(kind='sale', status='completed', cashier=self.cashier.name)
        self.post_cashier('rename', name='Renamed Cashier')
        response = self.post_cashier('rename', name='demo cashier')
        self.assertContains(response, 'Rename cashier completed.')
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.name, 'demo cashier')
        self.assertEqual(self.cashier.previous_names, ['Demo Cashier', 'Renamed Cashier'])
        self.assertEqual(self.assert_audit('cashier.rename').details['after']['name'], 'demo cashier')
        sale.refresh_from_db()
        self.assertEqual(sale.cashier, 'Demo Cashier')
        self.post_cashier('remove')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)

    def test_rejected_alias_claim_does_not_misclassify_unused_cashier_on_remove(self):
        sale = Document.objects.create(kind='sale', status='completed', cashier=self.cashier.name)
        self.post_cashier('rename', name='Renamed Cashier')
        self.post_cashier('add', name='demo cashier', pin='2468', pin_again='2468')
        duplicate = Cashier.objects.filter(name='demo cashier').first()
        if duplicate is not None:
            self.post_cashier('remove', duplicate)
            self.assertFalse(Cashier.objects.filter(pk=duplicate.pk).exists(),
                             'An unused new identity was retained because another cashier used its name.')
        self.assertIsNone(duplicate)
        self.post_cashier('add', name='Unrelated Cashier', pin='2468', pin_again='2468')
        unrelated = Cashier.objects.get(name='Unrelated Cashier')
        self.post_cashier('remove', unrelated)
        self.assertFalse(Cashier.objects.filter(pk=unrelated.pk).exists())
        self.assertIsNone(self.assert_audit('cashier.remove').details['after'])
        sale.refresh_from_db()
        self.assertEqual(sale.cashier, 'Demo Cashier')

    def test_reset_pin_accepts_four_five_and_six_digits_and_switch_can_use_them(self):
        register_client = Client()
        register_client.force_login(self.register)
        for pin in ('0192', '01928', '019283'):
            with self.subTest(length=len(pin)):
                self.post_cashier('pin', pin=pin, pin_again=pin)
                self.cashier.refresh_from_db()
                self.assertTrue(check_password(pin, self.cashier.pin_hash))
                self.assert_audit('cashier.pin')
                response = register_client.post('/cashier/', {'cashier': self.cashier.pk, 'pin': pin})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(register_client.session['active_cashier'], self.cashier.pk)

    def test_cashier_rename_preserves_sales_and_previous_names(self):
        sale = Document.objects.create(kind='sale', status='completed', cashier=self.cashier.name)
        self.post_cashier('rename', name='Renamed Cashier')
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.name, 'Renamed Cashier')
        self.assertEqual(self.cashier.previous_names, ['Demo Cashier'])
        sale.refresh_from_db()
        self.assertEqual(sale.cashier, 'Demo Cashier')
        entry = self.assert_audit('cashier.rename')
        self.assertEqual(entry.details['before']['name'], 'Demo Cashier')
        self.assertEqual(entry.details['after']['name'], 'Renamed Cashier')

    def test_general_sales_keeps_recorded_cashier_after_rename_and_remove(self):
        from reports.test_general_sales import line, sale
        from sales.models import Item

        product = Item.objects.create(sku='DEMO-CHAIR', name='Demo chair', price='10.00')
        document = sale(42, (2026, 11, 1), cashier=self.cashier.name)
        line(document, 1, product, '1', '10')
        self.post_cashier('rename', name='Renamed Cashier')
        self.post_cashier('remove')
        response = self.client.get('/reports/general-sales/', {
            'period': 'month', 'month': '2026-11', 'run': '1', 'submitted': '1', 'cols': ['cashier']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['report'].groups[0].rows[0]['cashier'], 'Demo Cashier')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)

    def test_remove_unused_cashier_hard_deletes(self):
        self.post_cashier('remove')
        self.assertFalse(Cashier.objects.filter(pk=self.cashier.pk).exists())
        self.assertIsNone(self.assert_audit('cashier.remove').details['after'])

    def test_remove_used_cashier_disables_without_deleting_sales(self):
        sale = Document.objects.create(kind='sale', status='completed', cashier=self.cashier.name)
        self.assertContains(self.post_cashier('remove'), 'has history and was disabled')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)
        self.assertTrue(Document.objects.filter(pk=sale.pk, cashier='Demo Cashier').exists())
        self.assert_audit('cashier.disable')

    def test_remove_after_repeated_renames_still_disables(self):
        Document.objects.create(kind='sale', status='completed', cashier='DEMO CASHIER')
        self.post_cashier('rename', name='Second Name')
        self.post_cashier('rename', name='Third Name')
        self.post_cashier('remove')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)
        self.assertEqual(self.cashier.previous_names, ['Demo Cashier', 'Second Name'])

    def test_pickup_history_prevents_cashier_deletion(self):
        Document.objects.create(kind='sale', status='completed', picked_up_by=self.cashier.name)
        self.post_cashier('remove')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)

    def test_payment_and_payment_change_history_prevent_deletion(self):
        order = Document.objects.create(kind='order', status='open')
        payment = OrderPayment.objects.create(order=order, amount='10.00', method='cash', cashier=self.cashier.name)
        self.post_cashier('remove')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)
        self.post_cashier('enable')
        payment.cashier = ''
        payment.save()
        OrderPaymentChange.objects.create(order=order, action='correct', cashier=self.cashier.name)
        self.post_cashier('remove')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)

    def test_cashier_disable_enable_and_disabled_identity_cannot_switch(self):
        self.post_cashier('disable')
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.active)
        register_client = Client()
        register_client.force_login(self.register)
        self.assertEqual(register_client.post('/cashier/', {'cashier': self.cashier.pk, 'pin': '0567'}).status_code, 400)
        self.post_cashier('enable')
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.active)
        self.assert_audit('cashier.enable')

    def test_secret_fields_are_absent_from_audit_responses_sessions_and_logs(self):
        with CapturedLogs() as logs:
            response = self.post_login('password', self.other, current_password=PASSWORD,
                                       new_password1=NEW_PASSWORD, new_password2=NEW_PASSWORD)
            pin_response = self.post_cashier('pin', pin='019283', pin_again='019283')
            wrong = self.post_login('rename', self.other, username='other.renamed', current_password='bad-secret-4782')
        stored = json.dumps(list(AuditEntry.objects.values('details')))
        session = json.dumps(dict(self.client.session))
        for secret in (PASSWORD, NEW_PASSWORD, '019283', 'bad-secret-4782', self.cashier.pin_hash):
            for surface in (response.content.decode(), pin_response.content.decode(), wrong.content.decode(), stored, session, logs.text):
                self.assertNotIn(secret, surface)
        self.assertNotIn('pin_hash', stored)
        self.assertNotIn('current_password', stored)

    def test_audit_records_actor_target_and_unspoofed_ip(self):
        self.client.post(self.login_url('rename'), {'username': 'renamed.register'},
                         REMOTE_ADDR='203.0.113.8', HTTP_X_FORWARDED_FOR='203.0.113.99')
        entry = self.assert_audit('login.rename')
        self.assertEqual(entry.actor_name, self.admin.username)
        self.assertEqual(entry.target, f'login:{self.register.pk}')
        self.assertEqual(entry.ip, '203.0.113.8')

    def test_audit_preserves_actor_name_after_actor_deleted(self):
        self.post_login('rename', username='renamed.register')
        entry = AuditEntry.objects.get()
        self.client.force_login(self.other)
        self.post_login('delete', self.admin, current_password=PASSWORD)
        entry.refresh_from_db()
        self.assertIsNone(entry.who)
        self.assertEqual(entry.actor_name, 'demo.admin')

    def test_audit_failure_rolls_back_login_cashier_and_deletion(self):
        for url, data in ((self.login_url('rename'), {'username': 'renamed.register'}),
                          (self.cashier_url('rename'), {'name': 'Renamed Cashier'}),
                          (self.cashier_url('remove'), {}), (self.login_url('delete'), {})):
            with self.subTest(url=url), mock.patch('accounts.user_views.audit', side_effect=RuntimeError('audit unavailable')):
                with self.assertRaises(RuntimeError):
                    self.client.post(url, data)
        self.register.refresh_from_db()
        self.cashier.refresh_from_db()
        self.assertEqual(self.register.username, 'demo.register')
        self.assertEqual(self.cashier.name, 'Demo Cashier')
        self.assertFalse(AuditEntry.objects.exists())

    def test_history_is_limited_to_last_fifty_user_entries(self):
        for number in range(55):
            AuditEntry.objects.create(who=self.admin, actor_name=self.admin.username, action='users.login.rename',
                                      target=f'login:history-{number}', details={'before': {}, 'after': {}})
        AuditEntry.objects.create(who=self.admin, actor_name=self.admin.username, action='other.event', target='other')
        response = self.client.get(reverse('users-settings'))
        self.assertEqual(len(response.context['entries']), 50)
        self.assertContains(response, 'login:history-54')
        self.assertNotContains(response, 'login:history-4<')
        self.assertNotContains(response, 'other.event')

    def test_cashier_and_audit_values_are_escaped(self):
        name = '<img src=x onerror=alert(1)>'
        self.post_cashier('rename', name=name)
        page = self.client.get(reverse('users-settings'))
        self.assertContains(page, '&lt;img src=x onerror=alert(1)&gt;')
        self.assertNotContains(page, name)
        self.assertNotContains(self.client.get(self.cashier_url('rename')), name)

    def test_post_redirect_get_on_success_and_failure(self):
        for data in ({'username': 'renamed.register'}, {'username': 'bad name'}):
            response = self.client.post(self.login_url('rename'), data)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(self.cashier_url('pin'), {'pin': 'wrong'}).status_code, 302)

    def test_csrf_enforced_on_every_mutation_and_valid_token_works(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for url in self.all_urls()[1:]:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {'username': 'forged'}).status_code, 403)
        self.assertFalse(AuditEntry.objects.exists())
        client.get(self.login_url('rename'))
        token = client.cookies['csrftoken'].value
        response = client.post(self.login_url('rename'), {'username': 'renamed.register', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        self.assert_audit('login.rename')


@skipUnless(connection.vendor == 'postgresql', 'Row locking requires PostgreSQL')
@override_settings(**TEST_SETTINGS)
class AdminConcurrencyTests(TransactionTestCase):
    def test_simultaneous_admins_cannot_disable_each_other(self):
        admins = [make_test_user(f'concurrent.admin{number}', ADMIN_GROUP, PASSWORD) for number in range(2)]
        barrier = Barrier(2)

        def disable(number):
            close_old_connections()
            try:
                client = Client()
                client.force_login(admins[number])
                barrier.wait(timeout=10)
                response = client.post(reverse('users-login-change', args=[admins[1 - number].pk, 'disable']),
                                       {'current_password': PASSWORD})
                return response.status_code, response.get('Location', '')
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(disable, range(2)))
        self.assertEqual(results.count((302, reverse('users-settings'))), 1)
        self.assertTrue(all(status == 403 or status == 302 and (
            url == reverse('users-settings') or url.startswith('/login/')) for status, url in results))
        self.assertEqual(get_user_model().objects.filter(is_active=True, groups__name=ADMIN_GROUP).count(), 1)
        self.assertEqual(AuditEntry.objects.count(), 1)
