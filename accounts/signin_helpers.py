# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test helpers for signing in TEST users. Admin Mode comes only from an admin sign-in, so tests that open
Reports / Server Status / Admin Override sign a TEST admin in; all identities and credentials are made up."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from .roles import ADMIN_GROUP, REGISTER_GROUP

# Sessions + request.user, without the login-required redirect (config.test_settings has no middleware).
AUTH_MIDDLEWARE = ['django.contrib.sessions.middleware.SessionMiddleware',
                   'django.contrib.auth.middleware.AuthenticationMiddleware']


def make_test_user(username, group, password=None):
    user, created = get_user_model().objects.get_or_create(username=username)
    if created or password:
        user.set_password(password or f'{username}-TEST-only')
        user.save()
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


def sign_in_admin(client, username='TEST Admin'):
    user = make_test_user(username, ADMIN_GROUP)
    client.force_login(user)
    return user


def sign_in_register(client, username='TEST Register'):
    user = make_test_user(username, REGISTER_GROUP)
    client.force_login(user)
    return user
