# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fork migrations never seed shop users, printer hardware, or private discount reasons."""
import importlib
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import TestCase

from sales.models import DiscountReason, Printer
from .models import Cashier

rename_group = importlib.import_module('accounts.migrations.0004_generic_register_group').rename_group


class FreshForkDataTests(TestCase):
    def test_migrated_database_has_no_shop_data(self):
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Cashier.objects.exists())
        self.assertFalse(Printer.objects.exists())
        self.assertFalse(DiscountReason.objects.exists())


class GenericRegisterGroupTests(TestCase):
    def test_existing_memberships_and_permissions_survive_the_rename(self):
        user = get_user_model().objects.create_user('TEST register')
        old = Group.objects.create(name='Register')
        user.groups.add(old)
        permission = Permission.objects.first()
        old.permissions.add(permission)
        rename_group(apps, SimpleNamespace(connection=connection))
        self.assertFalse(Group.objects.filter(name='Register').exists())
        current = Group.objects.get(name='register')
        self.assertEqual(current.pk, old.pk)
        self.assertTrue(user.groups.filter(pk=current.pk).exists())
        self.assertTrue(current.permissions.filter(pk=permission.pk).exists())

    def test_a_name_collision_merges_memberships_and_permissions(self):
        user = get_user_model().objects.create_user('TEST register')
        old = Group.objects.create(name='Register')
        current = Group.objects.create(name='register')
        user.groups.add(old)
        permission = Permission.objects.first()
        old.permissions.add(permission)
        rename_group(apps, SimpleNamespace(connection=connection))
        rename_group(apps, SimpleNamespace(connection=connection))
        self.assertFalse(Group.objects.filter(name='Register').exists())
        self.assertTrue(user.groups.filter(pk=current.pk).exists())
        self.assertTrue(current.permissions.filter(pk=permission.pk).exists())
