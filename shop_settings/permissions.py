# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register rights are login rights; a PIN never elevates the shared login."""
from functools import wraps

from django.conf import settings
from django.http import HttpResponseForbidden

from accounts.roles import is_admin, is_register
from .config import register_rules
from .models import REGISTER_RIGHTS


def register_can(request_or_user, right):
    if right not in REGISTER_RIGHTS:
        raise ValueError('Unknown register right.')
    user = getattr(request_or_user, 'user', request_or_user)
    if not getattr(user, 'is_authenticated', False):
        # Old tests exercise views without authentication middleware. Production never enables this harness flag.
        return bool(getattr(settings, 'REGISTER_RULES_TEST_ANONYMOUS', False))
    if not user.is_active:
        return False
    if is_admin(user):
        return True
    return bool(is_register(user) and getattr(register_rules(), right))


def refused(right):
    return HttpResponseForbidden('This register login cannot ' + right.replace('_', ' ') + '.', content_type='text/plain')


def sales_login_allowed(request):
    user = getattr(request, 'user', None)
    if not getattr(user, 'is_authenticated', False):
        # Only the explicit legacy test harness may also disable login enforcement.
        return bool(getattr(settings, 'REGISTER_RULES_TEST_ANONYMOUS', False)
                    and not getattr(settings, 'POS_REQUIRE_LOGIN', True))
    return bool(user.is_authenticated and user.is_active and (is_admin(user) or is_register(user)))


def sales_mutation_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == 'POST' and not sales_login_allowed(request):
            return HttpResponseForbidden('An active Admin or register login is required.', content_type='text/plain')
        return view(request, *args, **kwargs)
    return wrapped


def register_required(*rights):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            for right in rights:
                if not register_can(request, right):
                    return refused(right)
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


ITEM_FIELDS = {'edit_prices': ('price', 'price_2', 'price_3', 'price_4', 'price_5'),
               'edit_costs': ('average_cost', 'order_cost'), 'adjust_stock': ('on_hand',)}


def restrict_item_form(request, form):
    """Remove forbidden inputs before validation; reject forged writes rather than silently dropping them."""
    for right, names in ITEM_FIELDS.items():
        allowed = register_can(request, right)
        if right == 'edit_costs' and not register_can(request, 'see_costs'):
            allowed = False
        for name in names:
            if not allowed and request.method == 'POST' and name in request.POST:
                return refused(right)
            if not allowed or (right == 'edit_costs' and not register_can(request, 'see_costs')):
                form.fields.pop(name, None)
    return None
