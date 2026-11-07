# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server boundary tests: requests forge fields directly rather than relying on editor controls."""
import copy
import importlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import caches
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AuditEntry, Cashier
from accounts.roles import ADMIN_GROUP, REGISTER_GROUP
from purchasing.models import Department, Vendor
from sales.models import DiscountApproval, Document, InventoryAdjustment, Item
from .config import clear_cache, register_rules
from .models import Business, REGISTER_RIGHTS, RegisterRules
from .permissions import register_can

AUTH = ['django.contrib.sessions.middleware.SessionMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware']


@override_settings(MIDDLEWARE=AUTH, REGISTER_RULES_TEST_ANONYMOUS=False,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RegisterRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user('demo-admin', password='manager-pass')
        cls.admin.groups.add(Group.objects.get_or_create(name=ADMIN_GROUP)[0])
        cls.register = User.objects.create_user('demo-register', password='register-pass')
        cls.register.groups.add(Group.objects.get_or_create(name=REGISTER_GROUP)[0])
        cls.vendor = Vendor.objects.create(code='DEMO', company='Demo Supply')
        cls.department = Department.objects.create(code='DEMO', name='Demo Department')
        cls.item = Item.objects.create(sku='9001', name='Demo Stool', description='Demo Stool', price=100,
                                      average_cost=Decimal('13.37'), order_cost=Decimal('21.43'), on_hand=10,
                                      primary_vendor=cls.vendor, department=cls.department, tax_rate=0)

    def setUp(self):
        caches['register_overrides'].clear()
        RegisterRules.objects.all().delete()
        row = RegisterRules(pk=1)
        row.save()
        clear_cache()
        self.client.force_login(self.register)

    def rules(self, **values):
        row = RegisterRules.objects.get(pk=1)
        for name, value in values.items(): setattr(row, name, value)
        row.save()
        return row

    def payload(self, **values):
        body = dict(token=str(uuid.uuid4()), version=0, action='complete', customer_id=None,
                    lines=[dict(item_id=self.item.pk, quantity='1', price='100.00', tax_rate='0')],
                    payment_method='cash', tendered='100.00')
        body.update(values)
        return body

    def save_sale(self, body, kind='sale'):
        return self.client.post(f'/pos/{kind}/new/', json.dumps(body), content_type='application/json')

    def approve(self, body, username='demo-admin', password='manager-pass', kind='sale'):
        return self.client.post(f'/pos/{kind}/{body["token"]}/manager-override/',
                                json.dumps(dict(username=username, password=password, document=body)),
                                content_type='application/json')

    def test_new_shop_defaults_and_helper(self):
        row = register_rules()
        self.assertFalse(row.require_customer)
        self.assertFalse(row.require_discount_reason)
        for right in REGISTER_RIGHTS:
            self.assertEqual(register_can(self.register, right), right == 'receipt_discounts')
            self.assertTrue(register_can(self.admin, right))
        self.assertFalse(register_can(None, 'edit_items'))
        User = get_user_model()
        stranger = User.objects.create_user('demo-stranger', is_staff=True, is_superuser=True)
        self.assertFalse(register_can(stranger, 'edit_items'))
        self.register.is_active = False
        self.assertFalse(register_can(self.register, 'receipt_discounts'))

    def test_cache_invalidates_on_save_request_and_rollback(self):
        self.assertFalse(register_rules().edit_items)
        self.rules(edit_items=True)
        self.assertTrue(register_rules().edit_items)
        from django.db import transaction
        with transaction.atomic():
            self.rules(edit_items=False)
            self.assertFalse(register_rules().edit_items)
            transaction.set_rollback(True)
        self.assertTrue(register_rules().edit_items)
        RegisterRules.objects.update(edit_items=False)
        self.client.get('/health/')
        self.assertFalse(register_rules().edit_items)

    def test_settings_admin_only_and_saved_explicitly(self):
        self.assertEqual(self.client.get('/settings/register/').status_code, 403)
        self.assertEqual(self.client.post('/settings/register/', {}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get('/settings/register/'), 'Run setup again is unaffected')
        self.assertEqual(self.client.post('/settings/register/', {'version': 1, 'require_customer': 'on',
            'edit_items': 'on', 'discount_override_percent': 25}).status_code, 302)
        self.assertTrue(register_rules().require_customer)
        self.assertTrue(register_rules().edit_items)
        self.assertFalse(register_rules().receipt_discounts)
        self.assertEqual(self.client.post('/settings/register/', {'version': 2, 'discount_override_percent': 101}).status_code, 400)
        self.assertEqual(self.client.post('/settings/register/', {'version': 1, 'discount_override_percent': 0}).status_code, 400)

    def test_walkin_cash_card_and_gift_sales(self):
        issued = self.payload(lines=[dict(description='Gift certificate', item_name='GIFT CERTIFICATE',
                                         quantity='1', price='100', certificate_number='DEMO-GIFT', tax_rate='0')])
        self.assertEqual(self.save_sale(issued).status_code, 200)
        for method in ('cash', 'credit', 'debit', 'gift'):
            with self.subTest(method=method):
                body = self.payload(payment_method=method)
                if method == 'gift': body['payments'] = [{'method': 'gift', 'amount': '100', 'reference': 'DEMO-GIFT'}]
                response = self.save_sale(body)
                self.assertEqual(response.status_code, 200, response.content)
                doc = Document.objects.get(token=body['token'])
                self.assertIsNone(doc.customer_id)
                self.assertEqual(doc.customer_name, '')
                from sales.printing import bill_to, render_receipt
                self.assertEqual(bill_to(doc), '')
                self.assertGreater(render_receipt(doc).height, 0)

    def test_customer_requirement_on_and_unconditional_cases_off(self):
        self.rules(require_customer=True)
        self.assertEqual(self.save_sale(self.payload()).status_code, 400)
        self.rules(require_customer=False)
        cases = [('order', dict(action='save')), ('sale', dict(action='hold')),
                 ('sale', dict(payment_method='account')), ('sale', dict(payment_method='deposit')),
                 ('sale', dict(payment_method='store_credit')), ('sale', dict(hold_for_pickup=True)),
                 ('sale', dict(lines=[dict(account_payment=True, quantity='1', price='100')]))]
        for kind, values in cases:
            with self.subTest(kind=kind, values=values):
                response = self.save_sale(self.payload(**values), kind)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Document.objects.exists())

    def test_walkin_cash_refund_and_account_credit_refused(self):
        body = self.payload()
        self.assertEqual(self.save_sale(body).status_code, 200)
        line = Document.objects.get(token=body['token']).lines.get()
        refund = self.payload(lines=[dict(returned_from_line=line.pk, quantity='-1')], tendered='-100', payment_method='account')
        self.assertEqual(self.save_sale(refund).status_code, 400)
        refund['payment_method'] = 'cash'
        response = self.save_sale(refund)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(Document.objects.get(token=refund['token']).customer_id)

    def test_object_edit_views_and_json_rights(self):
        paths = {'edit_items': ['/purchasing/items/new/', f'/purchasing/items/{self.item.pk}/', '/purchasing/items/name-check/?name=Demo'],
                 'edit_vendors': ['/purchasing/vendors/new/', f'/purchasing/vendors/{self.vendor.pk}/', f'/records/vendors/{self.vendor.pk}/delete/'],
                 'edit_departments': ['/purchasing/departments/new/', f'/purchasing/departments/{self.department.pk}/', f'/records/departments/{self.department.pk}/delete/']}
        for right, urls in paths.items():
            for url in urls:
                with self.subTest(right=right, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.rules(**{right: True})
                    self.assertEqual(self.client.get(url).status_code, 200)
                    self.rules(**{right: False})
                    self.client.force_login(self.admin)
                    self.assertEqual(self.client.get(url).status_code, 200)
                    self.client.force_login(self.register)
        self.assertEqual(self.client.post('/pos/special-order-item/', '{}', content_type='application/json').status_code, 403)

    def test_forged_item_fields_and_admin_bypass(self):
        self.rules(edit_items=True)
        for right, field in [('edit_prices', 'price'), ('edit_costs', 'average_cost'), ('edit_costs', 'order_cost'), ('adjust_stock', 'on_hand')]:
            with self.subTest(right=right):
                url = f'/purchasing/items/{self.item.pk}/'
                self.assertEqual(self.client.post(url, {field: '1'}).status_code, 403)
                self.client.force_login(self.admin)
                self.assertEqual(self.client.get(url).status_code, 200)
                self.client.force_login(self.register)
        self.assertEqual(self.client.post(f'/purchasing/items/{self.item.pk}/', {'vendors-TOTAL_FORMS': '1', 'vendors-0-order_cost': '1'}).status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.price, 100)
        self.assertEqual(self.item.on_hand, 10)

    def test_costs_absent_in_html_and_json(self):
        self.rules(edit_items=True, edit_costs=True)
        InventoryAdjustment.objects.create(item=self.item, old_quantity=10, new_quantity=10,
                                            old_cost=Decimal('13.37'), new_cost=Decimal('21.43'))
        urls = ['/purchasing/items/', f'/purchasing/items/{self.item.pk}/', f'/purchasing/items/{self.item.pk}/details/',
                '/purchasing/items/?format=rows', '/purchasing/item-picker/?q=Demo', '/purchasing/lookup/items/?q=Demo']
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, '13.37')
                self.assertNotContains(response, '21.43')
        self.assertNotIn('cost', self.client.get('/purchasing/item-picker/?q=Demo').json()['results'][0])
        self.rules(see_costs=True)
        self.assertContains(self.client.get('/purchasing/items/'), '13.37')
        self.client.force_login(self.admin)
        self.rules(see_costs=False)
        self.assertContains(self.client.get('/purchasing/items/'), '13.37')

    def test_purchasing_and_stock_paths_closed(self):
        for url in ('/purchasing/po/new/', '/purchasing/voucher/new/', '/purchasing/history/', '/purchasing/truck/'):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.rules(see_costs=True, edit_costs=True)
        self.assertEqual(self.client.get('/purchasing/po/new/').status_code, 200)
        self.assertEqual(self.client.get('/purchasing/voucher/new/').status_code, 403)
        self.rules(adjust_stock=True)
        self.assertEqual(self.client.get('/purchasing/voucher/new/').status_code, 200)
        self.assertEqual(self.client.get('/purchasing/truck/').status_code, 200)

    def test_charge_account_toggle_and_customer_fields(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='demo', full_name='Demo Customer')
        url = f'/customers/{customer.pk}/charge-account/'
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(self.client.post(f'/customers/{customer.pk}/edit/', {'charge_account': 'on'}).status_code, 403)
        self.assertEqual(self.client.post('/customers/new/', {'account_limit': '500'}).status_code, 403)
        self.assertNotContains(self.client.get(f'/customers/{customer.pk}/edit/'), 'name="account_limit"')
        self.rules(manage_charge_accounts=True)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.rules(manage_charge_accounts=False)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url).status_code, 200)

    def test_pin_identity_never_changes_login_rights(self):
        from django.contrib.auth.hashers import make_password
        cashier = Cashier.objects.create(name='Demo Cashier', pin_hash=make_password('1234'))
        response = self.client.post('/cashier/', {'cashier': cashier.pk, 'pin': '1234'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['active_cashier'], cashier.pk)
        self.assertEqual(self.client.get('/purchasing/items/new/').status_code, 403)
        body = self.payload()
        self.assertEqual(self.save_sale(body).status_code, 200)
        self.assertEqual(Document.objects.get(token=body['token']).cashier, cashier.name)
        self.rules(receipt_discounts=False)
        self.assertEqual(self.save_sale(self.discounted()).status_code, 403)

    def mutation_snapshot(self):
        from customers.models import Customer
        from sales.models import Line, OrderPayment, StockMovement, Tender
        return [list(model.objects.order_by('pk').values()) for model in
                (Document, Line, OrderPayment, StockMovement, Tender, Item, Customer, DiscountApproval)]

    def test_sales_mutations_require_active_login_role(self):
        from customers.models import Customer
        from sales.services import save_document
        customer = Customer.objects.create(source_id='identity-demo', full_name='Demo Customer')
        stranger = get_user_model().objects.create_user('demo-no-role')
        removed = get_user_model().objects.create_user('demo-removed-role')
        removed.groups.add(Group.objects.get(name=REGISTER_GROUP))
        inactive = get_user_model().objects.create_user('demo-inactive', is_active=False)
        inactive.groups.add(Group.objects.get(name=REGISTER_GROUP))
        order = save_document(self.payload(customer_id=customer.pk, action='save'), 'order', uuid.uuid4())
        receipt = save_document(self.payload(customer_id=customer.pk), 'sale', uuid.uuid4())
        held = save_document(self.payload(customer_id=customer.pk, action='hold'), 'sale', uuid.uuid4())
        for identity in (stranger, removed, inactive, None):
            if identity:
                self.client.force_login(identity)
            else:
                self.client.logout()
            if identity == removed:
                removed.groups.clear()  # revoke within the existing login session
            for surface in ('sale', 'order', 'payment', 'fulfill', 'pickup', 'extend', 'delete'):
                with self.subTest(identity=identity, surface=surface):
                    before = self.mutation_snapshot()
                    if surface in ('sale', 'order'):
                        response = self.save_sale(self.payload(customer_id=customer.pk,
                            action='save' if surface == 'order' else 'complete'), surface)
                    else:
                        url, data = {
                            'payment': (f'/pos/order/{order.token}/payment/', dict(version=order.version,
                                token=str(uuid.uuid4()), amount='100', method='cash', tendered='100')),
                            'fulfill': (f'/pos/order/{order.token}/fulfill/', dict(version=order.version,
                                token=str(uuid.uuid4()), **{f'qty_{order.lines.get().pk}': '1'})),
                            'pickup': (f'/pos/sale/{receipt.token}/pickup/', dict(action='hold', version=receipt.version)),
                            'extend': (f'/pos/held/{held.token}/extend/', dict(hold_until='1-day')),
                            'delete': (f'/pos/held/{held.token}/delete/', {}),
                        }[surface]
                        response = self.client.post(url, data)
                    self.assertEqual(response.status_code, 403, response.content)
                    self.assertTrue(response['Content-Type'].startswith('text/plain'))
                    self.assertEqual(self.mutation_snapshot(), before)

    @override_settings(MIDDLEWARE=[*AUTH, 'accounts.middleware.RequireLoginMiddleware'], POS_REQUIRE_LOGIN=True)
    def test_revoked_register_cannot_consume_approval(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        approval = self.approve(body)
        self.assertEqual(approval.status_code, 200, approval.content)
        body['manager_approval_token'] = approval.json()['token']
        self.register.groups.clear()
        before = self.mutation_snapshot()
        response = self.save_sale(body)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(response['Content-Type'].startswith('text/plain'))
        self.assertEqual(self.mutation_snapshot(), before)

    def test_authorizer_rechecks_current_register_before_consuming(self):
        from django.core.exceptions import PermissionDenied
        from django.test import RequestFactory
        from sales.manager_override import authorizer
        from sales.services import save_document
        self.rules(receipt_discounts=False)
        body = self.discounted()
        body['manager_approval_token'] = self.approve(body).json()['token']
        request = RequestFactory().post('/pos/sale/new/')
        request.user = get_user_model().objects.get(pk=self.register.pk)
        request.session = self.client.session
        # Populate the per-request role cache, then revoke the role before consumption.
        self.assertFalse(register_can(request, 'receipt_discounts'))
        self.register.groups.clear()
        before = self.mutation_snapshot()
        with self.assertRaises(PermissionDenied):
            save_document(body, 'sale', body['token'],
                          discount_authorizer=authorizer(request, body, 'sale', body['token']))
        self.assertEqual(self.mutation_snapshot(), before)

    def test_admin_and_register_can_mutate_sales_orders_and_pickups(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='allowed-demo', full_name='Demo Customer')
        for identity in (self.register, self.admin):
            with self.subTest(identity=identity):
                self.client.force_login(identity)
                body = self.payload(customer_id=customer.pk)
                self.assertEqual(self.save_sale(body).status_code, 200)
                receipt = Document.objects.get(token=body['token'])
                self.assertEqual(self.client.post(f'/pos/sale/{receipt.token}/pickup/',
                    dict(action='hold', version=receipt.version)).status_code, 302)
                receipt.refresh_from_db()
                self.assertTrue(receipt.hold_for_pickup)
                body = self.payload(customer_id=customer.pk, action='save')
                self.assertEqual(self.save_sale(body, 'order').status_code, 200)
                order = Document.objects.get(token=body['token'])
                self.assertEqual(self.client.post(f'/pos/order/{order.token}/payment/',
                    dict(version=order.version, token=str(uuid.uuid4()), amount='100', method='cash', tendered='100')).status_code, 302)
                self.assertEqual(order.payments.count(), 1)
                order.refresh_from_db()
                self.assertEqual(self.client.post(f'/pos/order/{order.token}/fulfill/', dict(version=order.version,
                    token=str(uuid.uuid4()), **{f'qty_{order.lines.get().pk}': '1'})).status_code, 302)
                self.assertEqual(order.fulfillments.count(), 1)

    @override_settings(REGISTER_RULES_TEST_ANONYMOUS=True, POS_REQUIRE_LOGIN=True)
    def test_legacy_anonymous_flag_requires_login_enforcement_disabled(self):
        self.client.logout()
        self.assertEqual(self.save_sale(self.payload()).status_code, 403)
        with override_settings(POS_REQUIRE_LOGIN=False):
            self.assertEqual(self.save_sale(self.payload()).status_code, 200)
            self.client.force_login(get_user_model().objects.create_user('demo-harness-no-role'))
            self.assertEqual(self.save_sale(self.payload()).status_code, 403)

    def discounted(self, **values):
        return self.payload(lines=[dict(item_id=self.item.pk, quantity='1', price='80.00', tax_rate='0')], tendered='80.00', **values)

    def test_discount_reasons_optional_and_required(self):
        self.assertEqual(self.save_sale(self.discounted()).status_code, 200)
        self.rules(require_discount_reason=True)
        self.assertEqual(self.save_sale(self.discounted()).status_code, 400)
        body = self.payload(discount_percent='10', tendered='90')
        self.assertEqual(self.save_sale(body).status_code, 400)
        self.rules(require_discount_reason=False)
        self.assertEqual(self.save_sale(body).status_code, 200)

    def test_saved_line_reason_cannot_be_erased_in_receipt_or_order(self):
        from customers.models import Customer
        from sales.models import DiscountReason
        customer = Customer.objects.create(source_id='reason-demo', full_name='Demo Customer')
        DiscountReason.objects.create(name='Demo discount')
        self.rules(require_discount_reason=True)
        for kind in ('sale', 'order'):
            with self.subTest(kind=kind):
                body = self.discounted(customer_id=customer.pk, action='save' if kind == 'order' else 'complete')
                body['lines'][0]['discount_reason'] = 'Demo discount'
                self.assertEqual(self.save_sale(body, kind).status_code, 200)
                doc = Document.objects.get(token=body['token'])
                body['version'] = doc.version
                body['lines'][0].update(id=doc.lines.get().pk, discount_reason='')
                before = self.mutation_snapshot()
                response = self.save_sale(body, kind)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self.mutation_snapshot(), before)
                self.assertEqual(doc.lines.get().discount_reason, 'Demo discount')
                self.rules(require_discount_reason=False)
                self.assertEqual(self.save_sale(body, kind).status_code, 200)
                self.assertEqual(doc.lines.get().discount_reason, '')
                self.rules(require_discount_reason=True)

    def test_unchanged_historical_line_blanks_survive_reason_requirement(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='blank-demo', full_name='Demo Customer')
        for kind in ('sale', 'order'):
            with self.subTest(kind=kind):
                self.rules(require_discount_reason=False)
                body = self.discounted(customer_id=customer.pk, action='save' if kind == 'order' else 'complete')
                self.assertEqual(self.save_sale(body, kind).status_code, 200)
                doc = Document.objects.get(token=body['token'])
                body['version'] = doc.version
                body['lines'][0]['id'] = doc.lines.get().pk
                self.rules(require_discount_reason=True)
                self.assertEqual(self.save_sale(body, kind).status_code, 200)
                doc.refresh_from_db()
                self.assertEqual(doc.lines.get().discount_reason, '')
                body['version'] = doc.version
                body['lines'][0]['price'] = '70'
                body['tendered'] = '70'
                self.assertEqual(self.save_sale(body, kind).status_code, 400)

    @override_settings(MIDDLEWARE=[*AUTH, 'accounts.middleware.RequireLoginMiddleware'], POS_REQUIRE_LOGIN=True)
    def test_historical_blank_reason_cannot_transfer_to_replacement_item(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='blank-swap-demo', full_name='Demo Customer')
        replacement = Item.objects.create(sku='9002', name='Demo Replacement', description='Demo Replacement',
                                          price=100, on_hand=10, tax_rate=0)
        for kind in ('sale', 'order'):
            with self.subTest(kind=kind):
                self.rules(require_discount_reason=False)
                body = self.discounted(customer_id=customer.pk, action='save' if kind == 'order' else 'complete')
                self.assertEqual(self.save_sale(body, kind).status_code, 200)
                doc = Document.objects.get(token=body['token'])
                body['version'] = doc.version
                body['lines'][0].update(id=doc.lines.get().pk, item_id=replacement.pk)
                self.rules(require_discount_reason=True)
                before = self.mutation_snapshot()
                response = self.client.post(f'/pos/{kind}/{doc.token}/', json.dumps(body),
                                            content_type='application/json')
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self.mutation_snapshot(), before)
                self.rules(require_discount_reason=False)
                response = self.client.post(f'/pos/{kind}/{doc.token}/', json.dumps(body),
                                            content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(doc.lines.get().item_id, replacement.pk)

    @override_settings(MIDDLEWARE=[*AUTH, 'accounts.middleware.RequireLoginMiddleware'], POS_REQUIRE_LOGIN=True)
    def test_historical_blank_reason_preserves_custom_item_identity(self):
        from customers.models import Customer
        from sales.models import DiscountReason
        customer = Customer.objects.create(source_id='blank-custom-demo', full_name='Demo Customer')
        DiscountReason.objects.create(name='Demo discount')
        for kind in ('sale', 'order'):
            for original_item, replacement_item in ((None, None), (self.item.pk, None), (None, self.item.pk)):
                with self.subTest(kind=kind, original=original_item, replacement=replacement_item):
                    self.rules(require_discount_reason=False)
                    body = self.discounted(customer_id=customer.pk, action='save' if kind == 'order' else 'complete')
                    body['lines'][0].update(item_id=original_item, original_price='100',
                                            item_name='Demo merchandise', description='Demo merchandise')
                    self.assertEqual(self.save_sale(body, kind).status_code, 200)
                    doc = Document.objects.get(token=body['token'])
                    body['version'] = doc.version
                    body['lines'][0].update(id=doc.lines.get().pk, item_id=replacement_item)
                    self.rules(require_discount_reason=True)
                    before = self.mutation_snapshot()
                    url = f'/pos/{kind}/{doc.token}/'
                    response = self.client.post(url, json.dumps(body), content_type='application/json')
                    if original_item == replacement_item:
                        self.assertEqual(response.status_code, 200, response.content)
                        self.assertEqual(doc.lines.get().discount_reason, '')
                    else:
                        self.assertEqual(response.status_code, 400, response.content)
                        self.assertEqual(self.mutation_snapshot(), before)
                        body['lines'][0]['discount_reason'] = 'Demo discount'
                        response = self.client.post(url, json.dumps(body), content_type='application/json')
                        self.assertEqual(response.status_code, 200, response.content)
                        self.assertEqual(doc.lines.get().discount_reason, 'Demo discount')
                    saved = doc.lines.get()
                    self.assertEqual(saved.item_id, replacement_item)
                    self.assertEqual(saved.original_price, 100)
                    self.assertEqual(saved.price, 80)

    def test_return_and_pickup_keep_historical_blank_line_reasons(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='carry-reason-demo', full_name='Demo Customer')
        body = self.discounted(customer_id=customer.pk)
        self.assertEqual(self.save_sale(body).status_code, 200)
        original = Document.objects.get(token=body['token']).lines.get()
        body = self.discounted(customer_id=customer.pk, action='save')
        self.assertEqual(self.save_sale(body, 'order').status_code, 200)
        order = Document.objects.get(token=body['token'])
        self.client.post(f'/pos/order/{order.token}/payment/', dict(version=order.version,
            token=str(uuid.uuid4()), amount='80', method='cash', tendered='80'))
        order.refresh_from_db()
        self.rules(require_discount_reason=True, receipt_discounts=False)
        response = self.client.post(f'/pos/order/{order.token}/fulfill/', dict(version=order.version,
            token=str(uuid.uuid4()), **{f'qty_{order.lines.get().pk}': '1'}))
        self.assertEqual(response.status_code, 302)
        receipt = order.fulfillments.get()
        from sales.views import document_data
        carried = document_data(receipt)
        carried['action'] = 'complete'
        self.assertEqual(self.save_sale(carried).status_code, 200)
        returned = self.payload(customer_id=customer.pk,
            lines=[dict(returned_from_line=original.pk, quantity='-1')], tendered='-80')
        self.assertEqual(self.save_sale(returned).status_code, 200)

    def test_threshold_off_on_and_admin_bypass(self):
        self.assertEqual(self.save_sale(self.discounted()).status_code, 200)
        self.rules(discount_override_enabled=True, discount_override_percent=20)
        self.assertEqual(self.save_sale(self.discounted()).status_code, 200)  # equal is allowed
        self.rules(discount_override_percent=19)
        response = self.save_sale(self.discounted())
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()['override_required'])
        self.client.force_login(self.admin)
        self.assertEqual(self.save_sale(self.discounted()).status_code, 200)

    def test_receipt_and_line_percent_discount_require_approval(self):
        self.rules(discount_override_enabled=True, discount_override_percent=0)
        for body in [self.payload(discount_percent='1', tendered='99'),
                     self.payload(lines=[dict(item_id=self.item.pk, quantity='1', price='100', discount_percent='1', tax_rate='0')], tendered='99')]:
            self.assertEqual(self.save_sale(body).status_code, 403)
        self.assertFalse(Document.objects.exists())

    def test_without_discount_right_price_edits_require_override(self):
        self.rules(receipt_discounts=False)
        self.assertEqual(self.save_sale(self.discounted()).status_code, 403)
        self.assertEqual(self.save_sale(self.payload(lines=[dict(item_id=self.item.pk, quantity='1', price='110', tax_rate='0')], tendered='110')).status_code, 403)
        self.assertEqual(self.save_sale(self.payload()).status_code, 200)
        misc = self.payload(lines=[dict(description='Demo miscellaneous item', quantity='1', price='10', tax_rate='0')], tendered='10')
        self.assertEqual(self.save_sale(misc).status_code, 403)

    def test_override_refused_rate_limited_and_audited(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        for _ in range(5): self.assertEqual(self.approve(body, password='bad').status_code, 403)
        self.assertEqual(self.approve(body).status_code, 429)
        self.assertEqual(AuditEntry.objects.count(), 6)
        self.assertEqual(AuditEntry.objects.first().details['outcome'], 'rate_limited')
        stored = json.dumps(list(AuditEntry.objects.values('details')))
        self.assertNotIn('manager-pass', stored)
        self.assertFalse(DiscountApproval.objects.exists())

    def test_admin_name_rate_limit_applies_across_sessions(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        for _ in range(5):
            self.client.logout()
            self.client.force_login(self.register)
            self.assertEqual(self.approve(body, password='bad').status_code, 403)
        self.client.logout()
        self.client.force_login(self.register)
        self.assertEqual(self.approve(body).status_code, 429)

    def test_session_rate_limit_applies_across_admin_names(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        for n in range(5): self.assertEqual(self.approve(body, username=f'unknown-{n}').status_code, 403)
        self.assertEqual(self.approve(body).status_code, 429)

    def test_approval_one_time_records_approver_and_keeps_register_login(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        result = self.approve(body)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertFalse(Document.objects.exists())
        self.assertEqual(self.client.session['_auth_user_id'], str(self.register.pk))
        body['manager_approval_token'] = result.json()['token']
        self.assertEqual(self.save_sale(body).status_code, 200)
        doc = Document.objects.get(token=body['token'])
        self.assertEqual(doc.approved_by, self.admin)
        self.assertEqual(doc.lines.get().approved_by, self.admin)
        self.assertIsNotNone(DiscountApproval.objects.get().used_at)
        self.assertEqual(AuditEntry.objects.get().details['outcome'], 'approved')
        body['version'] = doc.version
        self.assertEqual(self.save_sale(body).status_code, 403)

    def approved_sale(self, *, receipt_discount=False):
        self.rules(receipt_discounts=False)
        body = self.payload(discount_percent='20', tendered='80') if receipt_discount else self.discounted()
        response = self.approve(body)
        self.assertEqual(response.status_code, 200, response.content)
        body['manager_approval_token'] = response.json()['token']
        self.assertEqual(self.save_sale(body).status_code, 200)
        doc = Document.objects.get(token=body['token'])
        self.assertEqual(doc.approved_by_id, self.admin.pk)
        self.assertEqual(doc.lines.get().approved_by_id, self.admin.pk)
        body.pop('manager_approval_token')
        body['version'] = doc.version
        body['lines'][0]['id'] = doc.lines.get().pk
        return body, doc

    def test_permitted_repricing_clears_obsolete_current_approver(self):
        for actor in (self.register, self.admin):
            for change in ('price', 'quantity', 'line_discount', 'receipt_discount'):
                with self.subTest(actor=actor, change=change):
                    self.client.force_login(self.register)
                    body, doc = self.approved_sale(receipt_discount=change == 'receipt_discount')
                    approvals = list(DiscountApproval.objects.order_by('pk').values())
                    audit = list(AuditEntry.objects.order_by('pk').values())
                    self.rules(receipt_discounts=True)
                    self.client.force_login(actor)
                    if change == 'price':
                        body['lines'][0]['price'] = '70'
                        body['tendered'] = '70'
                    elif change == 'quantity':
                        body['lines'][0]['quantity'] = '2'
                        body['tendered'] = '160'
                    elif change == 'line_discount':
                        body['lines'][0]['discount_percent'] = '10'
                        body['tendered'] = '90'
                    else:
                        body['discount_percent'] = '30'
                        body['tendered'] = '70'
                    response = self.save_sale(body)
                    self.assertEqual(response.status_code, 200, response.content)
                    doc.refresh_from_db()
                    self.assertIsNone(doc.approved_by_id)
                    self.assertIsNone(doc.lines.get().approved_by_id)
                    self.assertEqual(list(DiscountApproval.objects.order_by('pk').values()), approvals)
                    self.assertEqual(list(AuditEntry.objects.order_by('pk').values()), audit)

    def test_unchanged_and_notes_only_saves_preserve_current_approval(self):
        body, doc = self.approved_sale()
        self.rules(receipt_discounts=True)
        for actor, note in ((self.register, ''), (self.register, 'Demo note'), (self.admin, 'Demo admin note')):
            self.client.force_login(actor)
            body.update(version=doc.version, notes=note)
            self.assertEqual(self.save_sale(body).status_code, 200)
            doc.refresh_from_db()
            self.assertEqual(doc.approved_by_id, self.admin.pk)
            self.assertEqual(doc.lines.get().approved_by_id, self.admin.pk)

    def test_fresh_approval_replaces_current_attribution(self):
        body, doc = self.approved_sale()
        manager = get_user_model().objects.create_user('demo-second-manager', password='second-pass')
        manager.groups.add(Group.objects.get(name=ADMIN_GROUP))
        body['lines'][0]['price'] = '70'
        body['tendered'] = '70'
        response = self.approve(body, username=manager.username, password='second-pass')
        self.assertEqual(response.status_code, 200, response.content)
        body['manager_approval_token'] = response.json()['token']
        self.assertEqual(self.save_sale(body).status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.approved_by_id, manager.pk)
        self.assertEqual(doc.lines.get().approved_by_id, manager.pk)
        self.assertEqual(DiscountApproval.objects.filter(used_at__isnull=False).count(), 2)

    def test_approval_expiry_and_binding(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        raw = self.approve(body).json()['token']
        body['manager_approval_token'] = raw
        changed = copy.deepcopy(body)
        changed['lines'][0]['price'] = '70'
        changed['tendered'] = '70'
        self.assertEqual(self.save_sale(changed).status_code, 403)
        other = copy.deepcopy(body)
        other['token'] = str(uuid.uuid4())
        self.assertEqual(self.save_sale(other).status_code, 403)
        DiscountApproval.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.save_sale(body).status_code, 403)
        self.assertFalse(Document.objects.exists())

    def test_approval_bound_to_register_session(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        body['manager_approval_token'] = self.approve(body).json()['token']
        self.client.logout()
        self.client.force_login(self.register)
        self.assertEqual(self.save_sale(body).status_code, 403)

    def test_failed_save_does_not_consume_approval(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        body['manager_approval_token'] = self.approve(body).json()['token']
        with patch('sales.gifts.apply', side_effect=__import__('sales.services', fromlist=['WorkflowError']).WorkflowError('Try again.')):
            self.assertEqual(self.save_sale(body).status_code, 400)
        self.assertIsNone(DiscountApproval.objects.get().used_at)
        self.assertEqual(self.save_sale(body).status_code, 200)

    def test_migration_fresh_and_existing_saved_values(self):
        from types import SimpleNamespace
        migrate = importlib.import_module('shop_settings.migrations.0010_register_rules').seed_rules
        editor = SimpleNamespace(connection=connection)
        for business_exists, documents_exist in [(False, False), (True, False), (False, True)]:
            with self.subTest(business=business_exists, documents=documents_exist):
                Business.objects.all().delete()
                Document.objects.all().delete()
                RegisterRules.objects.all().delete()
                if business_exists: Business.objects.create(name='Demo Shop')
                if documents_exist: Document.objects.create(kind='sale', status='held')
                migrate(apps, editor)
                row = RegisterRules.objects.get()
                self.assertEqual(row.require_customer, business_exists or documents_exist)
                self.assertEqual(row.require_discount_reason, business_exists or documents_exist)
                for right in REGISTER_RIGHTS:
                    self.assertEqual(getattr(row, right), right == 'receipt_discounts' or business_exists or documents_exist)
                self.assertFalse(row.discount_override_enabled)


    def test_price_and_stock_edits_succeed_only_with_right_or_admin(self):
        self.rules(edit_items=True)
        base = dict(name='Demo Stool', item_type='inventory', description='Demo Stool', active='on',
                    tax_category='Taxable Sales', tax_rate='0')
        for right, field in [('edit_prices', 'price'), ('edit_costs', 'average_cost'), ('adjust_stock', 'on_hand')]:
            for is_admin in (False, True):
                with self.subTest(right=right, admin=is_admin):
                    if is_admin: self.client.force_login(self.admin)
                    else: self.rules(**{right: True}, see_costs=True)
                    self.item.refresh_from_db()
                    data = {**base, 'version': self.item.version, field: '2'}
                    response = self.client.post(f'/purchasing/items/{self.item.pk}/', data)
                    self.assertEqual(response.status_code, 302, response.content)
                    self.item.refresh_from_db()
                    self.assertEqual(getattr(self.item, field), Decimal('2'))
                    self.rules(**{right: False})
                    self.client.force_login(self.register)

    def test_approval_regular_price_comes_from_server_and_item_cannot_be_swapped(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='swap-demo', full_name='Demo Customer')
        self.rules(receipt_discounts=False)
        cheap = Item.objects.create(sku='9002', name='Demo Small Item', description='Demo Small Item',
                                    price='5', on_hand=10, tax_rate=0)
        body = self.payload(customer_id=customer.pk, lines=[dict(item_id=cheap.pk, quantity='1', price='5', tax_rate='0')], tendered='5')
        self.assertEqual(self.save_sale(body).status_code, 200)
        doc = Document.objects.get(token=body['token'])
        body['version'] = doc.version
        body['lines'] = [dict(id=doc.lines.get().pk, item_id=self.item.pk, quantity='1', price='5', tax_rate='0')]
        self.assertEqual(self.save_sale(body).status_code, 403)
        self.assertEqual(Document.objects.get(pk=doc.pk).lines.get().item_id, cheap.pk)
        forged = self.discounted()
        forged['lines'][0]['original_price'] = '80'
        self.assertEqual(self.save_sale(forged).status_code, 403)

    def test_approval_invalidated_by_catalog_price_or_manager_role_change(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        body['manager_approval_token'] = self.approve(body).json()['token']
        Item.objects.filter(pk=self.item.pk).update(price='200')
        self.assertEqual(self.save_sale(body).status_code, 403)
        Item.objects.filter(pk=self.item.pk).update(price='100')
        self.admin.groups.clear()
        self.assertEqual(self.save_sale(body).status_code, 403)

    def test_admin_password_is_verified_without_login_or_superuser_bypass(self):
        self.rules(receipt_discounts=False)
        User = get_user_model()
        User.objects.create_user('demo-superuser', password='manager-pass', is_superuser=True, is_staff=True)
        self.assertEqual(self.approve(self.discounted(), username='demo-superuser').status_code, 403)
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assertEqual(self.approve(self.discounted()).status_code, 403)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.register.pk))

    def test_receipt_approval_records_on_discounted_lines(self):
        self.rules(discount_override_enabled=True, discount_override_percent=0)
        body = self.payload(discount_percent='10', tendered='90')
        body['manager_approval_token'] = self.approve(body).json()['token']
        self.assertEqual(self.save_sale(body).status_code, 200)
        doc = Document.objects.get(token=body['token'])
        self.assertEqual(doc.approved_by_id, self.admin.pk)
        self.assertEqual(doc.lines.get().approved_by_id, self.admin.pk)

    def test_credential_endpoint_rejects_other_identities_and_bad_payloads(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        self.assertEqual(self.client.get(f'/pos/sale/{body["token"]}/manager-override/').status_code, 405)
        self.assertEqual(self.approve(body, kind='invalid').status_code, 400)
        self.assertEqual(self.client.post(f'/pos/sale/{body["token"]}/manager-override/', '[]', content_type='application/json').status_code, 400)
        self.client.force_login(self.admin)
        self.assertEqual(self.approve(body).status_code, 403)
        self.client.logout()
        self.assertEqual(self.approve(body).status_code, 403)

    def test_pdf_print_routes_and_voucher_post_do_not_leak_costs(self):
        token = uuid.uuid4()
        for url in (f'/purchasing/po/{token}/pdf/', f'/purchasing/voucher/{token}/pdf/'):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post('/purchasing/voucher/new/', {}).status_code, 403)
        self.assertEqual(self.client.post(f'/purchasing/po/{token}/print/', {}).status_code, 403)

    def test_charge_limit_override_forgery_is_forbidden(self):
        self.assertEqual(self.save_sale(self.payload(override_limit=True)).status_code, 403)
        self.assertEqual(self.client.post(f'/pos/order/{uuid.uuid4()}/payment/', {'override_limit': '1'}).status_code, 403)

    def test_indirect_special_order_catalog_edit_is_forbidden(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='special-demo', full_name='Demo Customer')
        special = Item.objects.create(sku='9003', name='SOLD Demo Item', description='SOLD Demo Item',
                                      price='100', on_hand=0, tax_rate=0)
        body = self.payload(customer_id=customer.pk, action='save', lines=[dict(item_id=special.pk, quantity='1', price='100', tax_rate='0')])
        self.assertEqual(self.save_sale(body, 'order').status_code, 200)
        doc = Document.objects.get(token=body['token'])
        body['version'] = doc.version
        body['lines'][0].update(id=doc.lines.get().pk, item_name='New Demo Name', description='New Demo Description')
        self.assertEqual(self.save_sale(body, 'order').status_code, 403)
        special.refresh_from_db()
        self.assertEqual(special.name, 'SOLD Demo Item')
        self.rules(edit_items=True)
        self.assertEqual(self.save_sale(body, 'order').status_code, 200)

    def test_walkin_render_does_not_emit_customer_heading(self):
        from sales.printing import Receipt, render_receipt
        body = self.payload()
        self.assertEqual(self.save_sale(body).status_code, 200)
        with patch.object(Receipt, 'text', autospec=True) as draw_text:
            render_receipt(Document.objects.get(token=body['token']))
        self.assertFalse(any(call.args[1] == 'BILL TO:' for call in draw_text.call_args_list))


    def test_deposit_and_later_pickup_hold_need_customer_even_on_legacy_documents(self):
        order = Document.objects.create(kind='order', status='open')
        response = self.client.post(f'/pos/order/{order.token}/payment/',
                                    {'version': order.version, 'token': str(uuid.uuid4()), 'amount': '10', 'method': 'cash'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('customer', response.url.lower())
        self.assertFalse(order.payments.exists())
        body = self.payload()
        self.assertEqual(self.save_sale(body).status_code, 200)
        doc = Document.objects.get(token=body['token'])
        response = self.client.post(f'/pos/sale/{doc.token}/pickup/', {'action': 'hold', 'version': doc.version})
        self.assertEqual(response.status_code, 302)
        self.assertIn('customer', response.url.lower())
        doc.refresh_from_db()
        self.assertFalse(doc.hold_for_pickup)

    def test_customerless_cancellation_refund_is_rejected_before_any_write(self):
        from sales.models import OrderPayment
        self.client.raise_request_exception = False
        for required in (False, True):
            self.rules(require_customer=required)
            for method in ('cash', 'credit', 'account'):
                with self.subTest(require_customer=required, method=method):
                    order = Document.objects.create(kind='order', status='open', customer=None, total='100')
                    OrderPayment.objects.create(order=order, amount='10', method='cash')
                    body = self.payload(token=str(order.token), version=order.version, action='cancel',
                        refund_deposit=True, deposit_refund_method=method, deposit_refund_amount='10')
                    before = self.mutation_snapshot()
                    # Isolate red probes so a successful cancellation cannot contaminate the next case.
                    from django.db import transaction
                    with transaction.atomic():
                        response = self.save_sale(body, 'order')
                        after = self.mutation_snapshot()
                        transaction.set_rollback(True)
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertIn('customer', response.json()['error'].lower())
                    self.assertEqual(after, before)

    def test_customer_linked_cancellation_refunds_still_work(self):
        from customers.models import Customer
        from sales.models import OrderPayment
        customer = Customer.objects.create(source_id='cancel-demo', full_name='Demo Customer', charge_account=True)
        for required in (False, True):
            self.rules(require_customer=required)
            for method in ('cash', 'credit', 'account'):
                with self.subTest(require_customer=required, method=method):
                    order = Document.objects.create(kind='order', status='open', customer=customer,
                        total='100', subtotal='100', tax='0')
                    OrderPayment.objects.create(order=order, amount='10', method='cash')
                    body = self.payload(token=str(order.token), version=order.version, action='cancel',
                        refund_deposit=True, deposit_refund_method=method, deposit_refund_amount='10')
                    response = self.save_sale(body, 'order')
                    self.assertEqual(response.status_code, 200, response.content)
                    order.refresh_from_db()
                    self.assertEqual(order.status, 'cancelled')
                    refund = order.payments.get(amount__lt=0)
                    self.assertEqual(refund.amount, Decimal('-10'))
                    self.assertEqual(refund.method, method)

    def test_override_rate_window_expires(self):
        self.rules(receipt_discounts=False)
        body = self.discounted()
        for _ in range(5): self.assertEqual(self.approve(body, password='bad').status_code, 403)
        self.assertEqual(self.approve(body).status_code, 429)
        import time
        future = timezone.now() + timedelta(seconds=901)
        with patch('time.time', return_value=time.time()+901), patch('django.core.cache.backends.db.tz_now', return_value=future):
            self.assertEqual(self.approve(body).status_code, 200)
        # A live cache entry containing old failures must not keep those failures in the rolling window.
        from sales.manager_override import digest, session_key
        from types import SimpleNamespace
        now = timezone.now().timestamp()
        session = self.client.session
        key = 'override:session:' + session_key(SimpleNamespace(session=session))
        session.save()
        caches['register_overrides'].set(key, [now-1000]*5, 900)
        caches['register_overrides'].set('override:admin:' + digest('demo-admin'), [now-1000]*5, 900)
        self.assertEqual(self.approve(body).status_code, 200)

    def test_csrf_is_required_for_manager_credentials(self):
        from django.test import Client
        with override_settings(MIDDLEWARE=[*AUTH, 'django.middleware.csrf.CsrfViewMiddleware']):
            client = Client(enforce_csrf_checks=True)
            client.force_login(self.register)
            body = self.discounted()
            response = client.post(f'/pos/sale/{body["token"]}/manager-override/',
                                   json.dumps(dict(username='demo-admin', password='manager-pass', document=body)),
                                   content_type='application/json')
            self.assertEqual(response.status_code, 403)
            self.assertFalse(DiscountApproval.objects.exists())

    def test_cost_and_price_values_cannot_be_obtained_from_permission_errors(self):
        for url, body in [(f'/purchasing/items/{self.item.pk}/', {'average_cost': '13.37'}),
                          ('/pos/special-order-item/', {'cost': '13.37'})]:
            response = self.client.post(url, body)
            self.assertEqual(response.status_code, 403)
            self.assertNotContains(response, '13.37', status_code=403)


    def test_threshold_uses_discount_percentage_before_cent_rounding(self):
        Item.objects.filter(pk=self.item.pk).update(price='0.05')
        self.rules(discount_override_enabled=True, discount_override_percent=10)
        body = self.payload(lines=[dict(item_id=self.item.pk, quantity='3', price='0.05', tax_rate='0')],
                            discount_percent='10', tendered='0.13')
        self.assertEqual(self.save_sale(body).status_code, 200)
        self.rules(discount_override_percent=0)
        body = self.payload(lines=[dict(item_id=self.item.pk, quantity='1', price='0.05', tax_rate='0', discount_percent='1')], tendered='0.05')
        self.assertEqual(self.save_sale(body).status_code, 403)


    def test_manual_pickup_receipt_price_edit_cannot_bypass_discount_right(self):
        from customers.models import Customer
        customer = Customer.objects.create(source_id='pickup-demo', full_name='Demo Customer')
        body = self.payload(customer_id=customer.pk, action='save')
        self.assertEqual(self.save_sale(body, 'order').status_code, 200)
        order = Document.objects.get(token=body['token'])
        response = self.client.post(f'/pos/order/{order.token}/payment/', {'version': order.version,
            'token': str(uuid.uuid4()), 'amount': '100', 'method': 'cash', 'tendered': '100'})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        response = self.client.post(f'/pos/order/{order.token}/fulfill/', {'version': order.version,
            'token': str(uuid.uuid4()), f'qty_{order.lines.get().pk}': '1'})
        self.assertEqual(response.status_code, 302)
        receipt = Document.objects.get(source_order=order)
        self.rules(receipt_discounts=False)
        changed = self.payload(token=str(receipt.token), version=receipt.version, customer_id=customer.pk,
            lines=[dict(id=receipt.lines.get().pk, item_id=self.item.pk, quantity='1', price='80', tax_rate='0')])
        self.assertEqual(self.save_sale(changed).status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.lines.get().price, 100)
        self.rules(require_discount_reason=True)
        self.assertEqual(self.save_sale(changed).status_code, 400)
        from sales.models import DiscountReason
        DiscountReason.objects.create(name='Demo discount')
        changed['lines'][0]['discount_reason'] = 'Demo discount'
        self.assertEqual(self.save_sale(changed).status_code, 403)

    def test_return_discount_cannot_offset_a_new_discount_for_permission_checks(self):
        body = self.payload(discount_percent='10', tendered='90')
        self.assertEqual(self.save_sale(body).status_code, 200)
        original = Document.objects.get(token=body['token']).lines.get()
        self.rules(receipt_discounts=False)
        exchange = self.payload(lines=[dict(returned_from_line=original.pk, quantity='-1'),
            dict(item_id=self.item.pk, quantity='1', price='100', tax_rate='0')], discount_percent='10',
            payments=[], tendered='0')
        self.assertEqual(self.save_sale(exchange).status_code, 403)
