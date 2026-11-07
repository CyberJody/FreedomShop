# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation helpers. Call only inside the view's atomic block and Admin group lock."""
import ipaddress
from math import ceil

from django.contrib.auth import authenticate
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from sales.models import Document, OrderPayment, OrderPaymentChange
from .models import AuditEntry
from .roles import ADMIN_GROUP, REGISTER_GROUP

REAUTH_FAILURE = 'Unable to verify your current admin password.'
REAUTH_LOCKED = 'Too many unsuccessful attempts. Please try again in 15 minutes.'


def admin_login(user):
    # Query current membership, never a request object's cached group names.
    return user.groups.filter(name=ADMIN_GROUP).exists()


def login_snapshot(user):
    return {'id': user.pk, 'username': user.username,
            'role': ADMIN_GROUP if admin_login(user) else REGISTER_GROUP, 'active': user.is_active}


def cashier_snapshot(cashier):
    return {'id': cashier.pk, 'name': cashier.name, 'active': cashier.active,
            'previous_names': list(cashier.previous_names)}


@sensitive_variables()
def verify_admin_password(request, actor, password):
    cache = caches['admin_reauthentication']
    key = f'admin-reauth:{actor.pk}'
    # The common Admin group row lock serializes the read/check/increment across
    # workers. DatabaseCache shares counters and expiry across those workers.
    now = timezone.now().timestamp()
    window = cache.get(key) or {'failures': 0, 'until': now + 15 * 60}
    if window['until'] <= now:
        window = {'failures': 0, 'until': now + 15 * 60}
    failures = window['failures']
    if failures >= 5:
        raise ValidationError(REAUTH_LOCKED)
    # Reserve the attempt before checking a credential. Fail closed if the cache
    # cannot persist it; DatabaseCache reports some write failures as False.
    window['failures'] = failures + 1
    # DatabaseCache rounds expiry down to whole seconds. Keep the counter until
    # the exact deadline; the window's timestamp determines eligibility.
    timeout = max(1, ceil(window['until'] - now) + 1)
    cache.set(key, window, timeout=timeout)
    if cache.get(key) != window:
        raise ValidationError(REAUTH_FAILURE)
    verified = authenticate(request, username=actor.get_username(), password=password)
    if verified is None or verified.pk != actor.pk:
        raise ValidationError(REAUTH_FAILURE)
    if failures == 0:
        # Successful confirmations must not start a window before the first failure.
        cache.delete(key)
        if cache.get(key) is not None:
            raise ValidationError(REAUTH_FAILURE)
        return
    window['failures'] = failures
    cache.set(key, window, timeout=timeout)
    if cache.get(key) != window:
        raise ValidationError(REAUTH_FAILURE)
    # A success does not erase earlier failures within the fixed 15-minute window.


def guard_login(actor, target, action, role=None):
    admin = admin_login(target)
    losing_admin = action in ('disable', 'delete') or action == 'role' and role != ADMIN_GROUP
    if admin and losing_admin:
        if target.is_active and not type(target).objects.filter(
                is_active=True, groups__name=ADMIN_GROUP).exclude(pk=target.pk).exists():
            raise ValidationError('Cannot disable, delete or demote the last active admin.')
        if target.pk == actor.pk:
            raise ValidationError('You cannot remove your own admin role, delete your login or disable yourself.')


def cashier_in_use(cashier):
    # Historical strings are deliberately left unchanged, including General Sales.
    names = [cashier.name, *cashier.previous_names]
    documents, pickups, payments, changes = Q(), Q(), Q(), Q()
    for name in names:
        documents |= Q(cashier__iexact=name)
        pickups |= Q(picked_up_by__iexact=name)
        payments |= Q(cashier__iexact=name)
        changes |= Q(cashier__iexact=name)
    return (Document.objects.filter(documents | pickups).exists()
            or OrderPayment.objects.filter(payments).exists()
            or OrderPaymentChange.objects.filter(changes).exists())


def audit(request, actor, action, target, before, after):
    try:
        ip = str(ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')))
    except ValueError:
        ip = None
    AuditEntry.objects.create(who=actor, actor_name=actor.get_username(), action=action, target=target,
                              details={'before': before, 'after': after}, ip=ip)
