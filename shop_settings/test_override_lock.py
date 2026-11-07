# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real PostgreSQL races for the manager approval failure-window locks."""
import copy
import json
import threading
import uuid
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import caches
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import AuditEntry
from accounts.roles import ADMIN_GROUP, REGISTER_GROUP
from sales import manager_override as mo
from sales.models import DiscountApproval, Document, Item
from .config import clear_cache
from .models import RegisterOverrideCache, RegisterRules


@skipUnless(connection.vendor == 'postgresql', 'Independent-connection PostgreSQL lock regression')
@override_settings(
    MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware',
                'django.contrib.auth.middleware.AuthenticationMiddleware',
                'accounts.middleware.RequireLoginMiddleware'],
    POS_REQUIRE_LOGIN=True, REGISTER_RULES_TEST_ANONYMOUS=False,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ManagerOverrideLockTests(TransactionTestCase):
    def setUp(self):
        caches['register_overrides'].clear()
        User = get_user_model()
        self.admin = User.objects.create_user('TEST-manager-a', password='synthetic-manager-pass')
        self.other = User.objects.create_user('TEST-manager-b', password='synthetic-manager-pass')
        self.register = User.objects.create_user('TEST-register', password='synthetic-register-pass')
        for user, group in ((self.admin, ADMIN_GROUP), (self.other, ADMIN_GROUP),
                            (self.register, REGISTER_GROUP)):
            user.groups.add(Group.objects.get_or_create(name=group)[0])
        RegisterRules.objects.all().delete()
        RegisterRules.objects.create(pk=1, require_customer=False, require_discount_reason=False,
                                     receipt_discounts=False)
        clear_cache()
        self.item = Item.objects.create(sku='TEST-9001', name='TEST item', description='TEST item',
                                        price=100, on_hand=10, tax_rate=0)
        self.body = dict(token=str(uuid.uuid4()), version=0, action='complete', customer_id=None,
                         lines=[dict(item_id=self.item.pk, quantity='1', price='80.00', tax_rate='0')],
                         payment_method='cash', tendered='80')
        self.client = self.new_client()

    def new_client(self):
        client = Client()
        client.force_login(self.register)
        session = client.session
        session['register_override_id'] = 'TEST-session-' + uuid.uuid4().hex
        session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        return client

    def clone(self, client):
        other = Client()
        other.cookies = copy.deepcopy(client.cookies)
        return other

    def approve(self, client, name=None, password='wrong'):
        return client.post(f'/pos/sale/{self.body["token"]}/manager-override/',
                           json.dumps(dict(username=name or self.admin.username, password=password,
                                           document=self.body)), content_type='application/json')

    def pair(self, jobs, wrapper):
        gate = threading.Barrier(2)
        results = [None, None]

        def run(i):
            connections.close_all()
            pid = None
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SET statement_timeout = 12000')
                    cursor.execute('SET lock_timeout = 10000')
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                gate.wait(timeout=10)
                with connection.execute_wrapper(wrapper):
                    result = jobs[i]()
                results[i] = dict(pid=pid, result=result, exception=None)
            except Exception as exc:
                results[i] = dict(pid=pid, exception=type(exc).__name__, message=str(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,), name=f'override-lock-{i}') for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'bounded worker did not finish')
        self.assertTrue(all(result is not None for result in results), results)
        self.assertNotEqual(results[0]['pid'], results[1]['pid'])
        return results

    def manager_race(self, shared, expired):
        cache = caches['register_overrides']
        for _ in range(3):
            self.assertEqual(self.approve(self.client).status_code, 403)
        if shared == 'admin':
            other = self.new_client()
            names = [self.admin.username, self.admin.username.upper()]
            key = 'override:admin:' + mo.digest(self.admin.username.casefold())
        else:
            other = self.clone(self.client)
            names = [self.admin.username, self.other.username]
            key = 'override:session:' + mo.digest(self.client.session['register_override_id'])
        clients = [self.clone(self.client), other]
        lock_key = key + ':lock'
        if expired:
            # Real abandoned lock row, without changing cache returns or the failure history.
            cache.set(lock_key, 'TEST-abandoned-owner', timeout=-60)
            with connection.cursor() as cursor:
                cursor.execute('SELECT expires FROM shop_settings_registeroverridecache WHERE cache_key=%s',
                               [cache.make_key(lock_key)])
                self.assertIsNotNone(cursor.fetchone())
        updates = threading.Barrier(2)
        auth_entered, release_auth = threading.Event(), threading.Event()
        auth_calls = []
        real_auth = mo.authenticate

        def auth(*args, **kwargs):
            auth_calls.append(kwargs['username'])
            # Correct serialization admits one credential check. Keep it inside the
            # critical section until the actual competing request returns busy.
            auth_entered.set()
            self.assertTrue(release_auth.wait(timeout=10))
            return real_auth(*args, **kwargs)

        def wrapper(execute, sql, params, many, context):
            if (expired and sql.startswith('UPDATE') and 'shop_settings_registeroverridecache' in sql
                    and cache.make_key(lock_key) in (params or [])):
                updates.wait(timeout=10)
            return execute(sql, params, many, context)

        def attempt(i):
            if not expired and i == 1:
                self.assertTrue(auth_entered.wait(timeout=10))
            response = self.approve(clients[i], names[i])
            if response.status_code == 429:
                release_auth.set()
            return response.status_code

        with patch.object(mo, 'authenticate', side_effect=auth):
            results = self.pair([lambda: attempt(0), lambda: attempt(1)], wrapper)
        before_retry = cache.get(key)
        self.assertTrue(all(r['exception'] is None for r in results), results)
        self.assertEqual(sorted(r['result'] for r in results), [403, 429])
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(len(before_retry), 4)
        busy = next(i for i, result in enumerate(results) if result['result'] == 429)
        retry_calls = []

        def retry_auth(*args, **kwargs):
            retry_calls.append(kwargs['username'])
            return real_auth(*args, **kwargs)

        with patch.object(mo, 'authenticate', side_effect=retry_auth):
            self.assertEqual(self.approve(clients[busy], names[busy]).status_code, 403)
        self.assertEqual(len(retry_calls), 1)
        actual = cache.get(key)
        auth_after = []

        def observe_auth(*args, **kwargs):
            auth_after.append(kwargs['username'])
            return real_auth(*args, **kwargs)

        with patch.object(mo, 'authenticate', side_effect=observe_auth):
            valid_name = self.admin.username if shared == 'admin' else names[busy]
            valid = self.approve(clients[busy], valid_name, 'synthetic-manager-pass')
        print('LOCK OBSERVATION', json.dumps(dict(test=self._testMethodName, shared=shared,
              expired=expired, results=results, auth_calls=auth_calls, counter_before_retry=before_retry,
              retry_auth_calls=retry_calls, counter=actual,
              subsequent_valid_status=valid.status_code, subsequent_auth_calls=auth_after,
              grants=DiscountApproval.objects.count(), documents=Document.objects.count(),
              outcomes=list(AuditEntry.objects.order_by('pk').values_list('details', flat=True)))), flush=True)
        self.assertTrue(all(r['exception'] is None for r in results), results)
        self.assertEqual(len(actual), 5, 'Both real wrong authentications must survive in the shared history')
        self.assertEqual(valid.status_code, 429)
        self.assertEqual(auth_after, [])
        self.assertFalse(DiscountApproval.objects.exists())
        self.assertFalse(Document.objects.exists())
        outcomes = [entry['outcome'] for entry in AuditEntry.objects.values_list('details', flat=True)]
        self.assertEqual(outcomes.count('refused'), 5)
        self.assertEqual(outcomes.count('busy'), 1)
        self.assertEqual(outcomes.count('rate_limited'), 1)

    def test_expired_lock_shared_normalized_name_preserves_failures(self):
        self.manager_race('admin', True)

    def test_expired_lock_shared_session_preserves_failures(self):
        self.manager_race('session', True)

    def test_absent_lock_shared_normalized_name_is_busy_without_authentication(self):
        self.manager_race('admin', False)

    def test_absent_lock_shared_session_is_busy_without_authentication(self):
        self.manager_race('session', False)

    def lock_keys(self):
        return ['override:admin:' + mo.digest(self.admin.username.casefold()) + ':lock',
                'override:session:' + mo.digest(self.client.session['register_override_id']) + ':lock']

    def acquisition_race(self, expired):
        cache = caches['register_overrides']
        for key in self.lock_keys():
            with self.subTest(key=key, expired=expired):
                if expired:
                    cache.set(key, 'TEST-abandoned-owner', timeout=-60)
                gate = threading.Barrier(2)

                def wrapper(execute, sql, params, many, context):
                    operation = 'UPDATE' if expired else 'INSERT'
                    if (sql.startswith(operation) and 'shop_settings_registeroverridecache' in sql
                            and cache.make_key(key) in (params or [])):
                        gate.wait(timeout=10)
                    return execute(sql, params, many, context)

                results = self.pair([
                    lambda: mo._acquire_rate_lock(caches['register_overrides'], key, 'TEST-owner-a'),
                    lambda: mo._acquire_rate_lock(caches['register_overrides'], key, 'TEST-owner-b')], wrapper)
                print('ACQUIRE OBSERVATION', json.dumps(dict(key=key, expired=expired, results=results)), flush=True)
                self.assertTrue(all(r['exception'] is None for r in results), results)
                self.assertEqual(sorted(r['result'] for r in results), [False, True])
                winner = 'TEST-owner-a' if results[0]['result'] else 'TEST-owner-b'
                self.assertEqual(cache.get(key), winner)
                loser = 'TEST-owner-b' if winner == 'TEST-owner-a' else 'TEST-owner-a'
                mo._release_rate_lock(cache, key, loser)
                self.assertEqual(cache.get(key), winner)
                mo._release_rate_lock(cache, key, winner)
                self.assertIsNone(cache.get(key))

    def test_atomic_absent_acquisition_has_one_owner_for_both_keys(self):
        self.acquisition_race(False)

    def test_atomic_expired_acquisition_has_one_owner_for_both_keys(self):
        self.acquisition_race(True)

    def test_release_cannot_delete_replacement_owner_for_either_key(self):
        cache = caches['register_overrides']
        for key in self.lock_keys():
            with self.subTest(key=key):
                self.assertTrue(mo._acquire_rate_lock(cache, key, 'TEST-old-owner'))
                releasing, replaced = threading.Event(), threading.Event()

                def wrapper(execute, sql, params, many, context):
                    if (threading.current_thread().name == 'override-lock-0' and sql.startswith('DELETE')
                            and 'shop_settings_registeroverridecache' in sql
                            and cache.make_key(key) in (params or [])):
                        releasing.set()
                        self.assertTrue(replaced.wait(timeout=10))
                    return execute(sql, params, many, context)

                def takeover():
                    self.assertTrue(releasing.wait(timeout=10))
                    RegisterOverrideCache.objects.filter(cache_key=cache.make_key(key)).update(
                        expires=timezone.now() - timedelta(seconds=60))
                    acquired = mo._acquire_rate_lock(caches['register_overrides'], key, 'TEST-new-owner')
                    replaced.set()
                    return acquired

                results = self.pair([
                    lambda: mo._release_rate_lock(caches['register_overrides'], key, 'TEST-old-owner'),
                    takeover], wrapper)
                print('RELEASE OBSERVATION', json.dumps(dict(key=key, results=results)), flush=True)
                self.assertTrue(all(r['exception'] is None for r in results), results)
                self.assertTrue(results[1]['result'])
                self.assertEqual(cache.get(key), 'TEST-new-owner')
                mo._release_rate_lock(cache, key, 'TEST-new-owner')
                self.assertIsNone(cache.get(key))

    def test_existing_owner_is_busy_and_partial_acquisition_is_released(self):
        cache = caches['register_overrides']
        for key in self.lock_keys():
            with self.subTest(key=key):
                self.assertTrue(mo._acquire_rate_lock(cache, key, 'TEST-existing-owner'))
                with patch.object(mo, 'authenticate', wraps=mo.authenticate) as auth:
                    response = self.approve(self.client)
                self.assertEqual(response.status_code, 429)
                auth.assert_not_called()
                self.assertEqual(cache.get(key), 'TEST-existing-owner')
                for other in set(self.lock_keys()) - {key}:
                    self.assertIsNone(cache.get(other))
                mo._release_rate_lock(cache, key, 'TEST-existing-owner')
                self.assertFalse(DiscountApproval.objects.exists())


class RateLockOwnershipTests(TestCase):
    def test_lock_lifecycle_preserves_history_for_both_policy_keys(self):
        cache = caches['register_overrides']
        cache.clear()
        for key in ('override:admin:TEST-normalized-name', 'override:session:TEST-session'):
            with self.subTest(key=key):
                history = [timezone.now().timestamp()] * 3
                cache.set(key, history, 900)
                lock = key + ':lock'
                self.assertTrue(mo._acquire_rate_lock(cache, lock, 'TEST-owner-a'))
                self.assertFalse(mo._acquire_rate_lock(cache, lock, 'TEST-owner-b'))
                mo._release_rate_lock(cache, lock, 'TEST-owner-b')
                self.assertEqual(cache.get(lock), 'TEST-owner-a')
                RegisterOverrideCache.objects.filter(cache_key=cache.make_key(lock)).update(
                    expires=timezone.now() - timedelta(seconds=60))
                self.assertTrue(mo._acquire_rate_lock(cache, lock, 'TEST-owner-b'))
                mo._release_rate_lock(cache, lock, 'TEST-owner-a')
                self.assertEqual(cache.get(lock), 'TEST-owner-b')
                mo._release_rate_lock(cache, lock, 'TEST-owner-b')
                self.assertIsNone(cache.get(lock))
                self.assertEqual(cache.get(key), history)
