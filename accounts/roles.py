# SPDX-License-Identifier: AGPL-3.0-or-later
"""Login roles.

Two kinds of Django login, told apart by group membership:

- ``Admin`` group: the owners. Signed in = the active cashier automatically (no PIN) and Admin Mode always on.
- ``register`` group: the one shared register login (default username ``register``). Staff work under it and
  identify themselves through the Cashier PIN pop-up.

PIN staff are ``accounts.models.Cashier`` rows, never Django users, so they cannot sign in anywhere. The main login
screen offers only the admins and the register login in a dropdown; any other username is refused before
authentication is even attempted.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

ADMIN_GROUP = 'Admin'
REGISTER_GROUP = 'register'


def group(name):
    return Group.objects.get_or_create(name=name)[0]


def _in_group(user, name):
    if not user or not user.is_authenticated:
        return False
    names = getattr(user, '_wc_groups', None)
    if names is None:
        # One query per request: the request's user object is rebuilt on every request.
        names = user._wc_groups = set(user.groups.values_list('name', flat=True))
    return name in names


def is_admin(user):
    return _in_group(user, ADMIN_GROUP)


def is_register(user):
    """The shared register login: the only login that may activate PIN cashiers. Admins are never register users."""
    return _in_group(user, REGISTER_GROUP) and not is_admin(user)


def display_name(user):
    return (user.get_full_name() or user.get_username()).strip()


def login_users():
    """Active logins allowed at the sign-in screen: admins first (by name), then the register login(s)."""
    User = get_user_model()
    admins = list(User.objects.filter(is_active=True, groups__name=ADMIN_GROUP).order_by('username'))
    registers = [u for u in User.objects.filter(is_active=True, groups__name=REGISTER_GROUP).order_by('username')
                 if u not in admins]
    return admins + registers


def login_choices():
    return [(user.get_username(), display_name(user)) for user in login_users()]
