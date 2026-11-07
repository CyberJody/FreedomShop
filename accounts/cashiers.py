# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is ringing: the active cashier for the signed-in session.

- Admin logins ARE the cashier (their own name, no PIN) —.
- Under the shared register login, staff pick their name in the Cashier pop-up and type their 4–6 digit PIN. The PIN is
  checked against the chosen cashier only. A PIN never authenticates a Django user and never grants Admin Mode.
- Lockouts: 10 wrong PINs in 15 minutes lock that cashier; 20 wrong PINs in 15 minutes from one signed-in session lock
  the pop-up at that register, so one register cannot try every name in turn.
"""
import hashlib
import re
from datetime import timedelta
from typing import NamedTuple, Optional

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from .models import Cashier, LoginWindow
from .roles import display_name, is_admin, is_register

SESSION_KEY = 'active_cashier'
LAST_SEEN = 'cashier_last_seen'
USER_LIMIT, REGISTER_LIMIT, WINDOW_MINUTES = 10, 20, 15


class ActiveCashier(NamedTuple):
    name: str
    pk: Optional[int]  # Cashier row for PIN staff; None for an admin login
    admin: bool


def clear(request):
    request.session.pop(SESSION_KEY, None)
    request.session.pop(LAST_SEEN, None)


def active(request):
    user = getattr(request, 'user', None)
    session = getattr(request, 'session', None)
    if not user or not user.is_authenticated or session is None:
        return None
    if is_admin(user):
        return ActiveCashier(display_name(user), None, True)
    pk = session.get(SESSION_KEY)
    if not pk:
        return None
    now = timezone.now().timestamp()
    timeout = getattr(settings, 'CASHIER_TIMEOUT_MINUTES', 0)
    if timeout and now - session.get(LAST_SEEN, 0) >= timeout * 60:
        clear(request)
        return None
    cashier = Cashier.objects.filter(pk=pk, active=True).first()
    if cashier is None:
        clear(request)
        return None
    session[LAST_SEEN] = now
    return ActiveCashier(cashier.name, cashier.pk, False)


def pin_cashiers(request):
    """Names offered in the pop-up: active PIN staff, only for the register login (lazy queryset)."""
    user = getattr(request, 'user', None)
    if not user or not is_register(user):
        return Cashier.objects.none()
    return Cashier.objects.filter(active=True).order_by('name')


def context(request):
    return {'active_cashier': active(request), 'pin_cashiers': pin_cashiers(request),
            'register_login': bool(getattr(request, 'user', None)) and is_register(request.user)}


def _window(key):
    window, _ = LoginWindow.objects.get_or_create(key=key)
    window = LoginWindow.objects.select_for_update().get(pk=window.pk)
    if window.started_at < timezone.now() - timedelta(minutes=WINDOW_MINUTES):
        window.failures = 0
        window.started_at = timezone.now()
    return window


def _register_key(request):
    source = request.session.session_key or f'user:{request.user.pk}'
    # LoginWindow.key is 64 chars: prefix + 40 hex digits (Postgres enforces the length, SQLite does not).
    return 'pin-register:' + hashlib.sha256(source.encode()).hexdigest()[:40]


@never_cache
@require_POST
@sensitive_post_parameters('pin')
@sensitive_variables()
def switch(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return JsonResponse({'error': 'Sign in before choosing a cashier.'}, status=401)
    if not is_register(user):
        who = 'You are signed in as an admin and are the cashier automatically.' if is_admin(user) \
            else 'PIN cashiers are only available under the register login.'
        return JsonResponse({'error': who}, status=403)
    if request.POST.get('action') == 'clear':
        clear(request)
        return JsonResponse({'cashier': 'No cashier'})
    pin = request.POST.get('pin', '')
    try:
        chosen = Cashier.objects.filter(pk=int(request.POST.get('cashier', '')), active=True).first()
    except (TypeError, ValueError):
        chosen = None
    found = None
    with transaction.atomic():
        register = _window(_register_key(request))
        if register.failures >= REGISTER_LIMIT:
            return JsonResponse({'error': 'Too many unsuccessful attempts at this register. '
                                          'Please try again in 15 minutes.'}, status=429)
        if chosen is None:
            register.failures += 1
            register.save(update_fields=['failures', 'started_at'])
            return JsonResponse({'error': 'Choose your name from the list.'}, status=400)
        person = _window(f'pin:{chosen.pk}')
        if person.failures >= USER_LIMIT:
            return JsonResponse({'error': f'Too many unsuccessful attempts for {chosen.name}. '
                                          'Please try again in 15 minutes.'}, status=429)
        if re.fullmatch(r'[0-9]{4,6}', pin) and check_password(pin, chosen.pin_hash):
            found = chosen
        person.failures = 0 if found else person.failures + 1
        register.failures = 0 if found else register.failures + 1
        person.save(update_fields=['failures', 'started_at'])
        register.save(update_fields=['failures', 'started_at'])
    if found is None:
        return JsonResponse({'error': 'PIN not recognized'}, status=400)
    request.session[SESSION_KEY] = found.pk
    request.session[LAST_SEEN] = timezone.now().timestamp()
    return JsonResponse({'cashier': found.name})
