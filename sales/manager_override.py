# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manager approval authorizes one server-computed sale, never an administrator session.

Tokens are random, stored hashed, and consumed under a row lock in the sale transaction. The
fingerprint includes all submitted content and computed prices, quantities, discount shares and total.
Rate counters use a shared database cache; short cache locks serialize checks across workers.
"""
import base64
import hashlib
import json
import pickle
import secrets
from datetime import timedelta

from django.contrib.auth import authenticate
from django.core.cache import caches
from django.core.exceptions import PermissionDenied
from django.db import connections, router
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.models import AuditEntry
from accounts.roles import is_admin, is_register
from shop_settings.config import register_rules
from shop_settings.permissions import register_can
from .models import DiscountApproval
from .services import WorkflowError, save_document


class ApprovalRequired(Exception):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def session_key(request):
    # Separate from the signed session cookie (which changes whenever session content is saved).
    if not request.session.get('register_override_id'):
        request.session['register_override_id'] = secrets.token_urlsafe(32)
    return digest(request.session['register_override_id'])


def authorizer(request, data, kind, token, *, probe=False):
    def authorize(document, lines, discount, percent, total):
        from .discounts import eligible
        rules = register_rules()
        # Live edits need line reasons; trusted import/pickup services can carry historical blanks.
        if rules.require_discount_reason:
            # A saved blank belongs to the same merchandise, including custom items (item_id=None).
            previous = {line.pk: (line.item_id, line.original_price, line.price, line.discount_percent)
                        for line in document.lines.all() if not line.discount_reason} if document else {}
            for line in lines:
                if (line.discounted or line.discount_percent) and not line.discount_reason and not line.returned_from_line_id:
                    if previous.get(line.pk) != (line.item_id, line.original_price, line.price, line.discount_percent):
                        raise WorkflowError('Choose a reason for the line discount.')
        if register_can(request, 'receipt_discounts') and is_admin(getattr(request, 'user', None)):
            return None
        def pricing(line):
            return (line.original_price, line.price, line.quantity, line.receipt_discount, line.tax_rate)
        prior_pricing = {line.pk: pricing(line) for line in document.lines.all()} if document else {}
        # Pickups carry already-agreed order discounts. Only unchanged inherited pricing is
        # exempt: a manual price/quantity edit on a pickup receipt still needs authorization.
        targets = [line for line in lines if eligible(line) and not (
            line.source_order_line_id and prior_pricing.get(line.pk) == pricing(line))]
        # A miscellaneous/unpriced item's entered price has no trusted catalog baseline.
        # Without receipt price rights, entering it needs approval too.
        edited = any(line.item_id is None or line.price != line.original_price or line.item.price is None
                     for line in targets)
        # A return's negative discount must not cancel out a new merchandise discount.
        discounted = bool(targets and percent > 0) or any(
            line.discounted or line.discount_percent > 0 or line.receipt_discount > 0 for line in targets)
        # Compare percentages before cent rounding: a 10% discount stays 10% even when its
        # allocated penny is a larger fraction of a cheap line. A tiny line percentage must
        # still count when its rounded extension has not changed. Do not dilute a discounted
        # line with full-price merchandise elsewhere on the receipt.
        line_percents = []
        for line in targets:
            if not line.original_price or line.quantity <= 0:
                continue
            reduction = (line.original_price - line.price) * 100 / line.original_price
            if percent:
                reduction = 100 - (100 - reduction) * (100 - percent) / 100
            else:
                reduction += line.receipt_discount * 100 / (line.quantity * line.original_price)
            line_percents.append(reduction)
        maximum = max([percent, *(line.discount_percent for line in targets), *line_percents])
        needs = ((edited or discounted) and not register_can(request, 'receipt_discounts')) or (
            discounted and rules.discount_override_enabled and maximum > rules.discount_override_percent)
        if not needs:
            return None
        submitted = {key: value for key, value in data.items() if key != 'manager_approval_token'}
        computed = [(line.item_id, line.pk, str(line.quantity), str(line.original_price), str(line.price),
                     str(line.receipt_discount), str(line.tax_rate)) for line in lines]
        fingerprint = digest(json.dumps([kind, str(token), submitted, computed, str(discount), str(total)],
                                        sort_keys=True, separators=(',', ':')))
        if probe:
            raise ApprovalRequired(fingerprint)
        raw = data.get('manager_approval_token')
        if not isinstance(raw, str) or len(raw) > 100:
            raise ApprovalRequired(fingerprint)
        # A grant never survives loss of the register role, even if this request cached it.
        from django.contrib.auth import get_user_model
        from accounts.roles import ADMIN_GROUP, REGISTER_GROUP
        user = getattr(request, 'user', None)
        if not getattr(user, 'is_authenticated', False) or not get_user_model().objects.filter(
                pk=user.pk, is_active=True, groups__name=REGISTER_GROUP).exclude(groups__name=ADMIN_GROUP).exists():
            raise PermissionDenied('An active register login is required for manager approval.')
        approval = DiscountApproval.objects.select_for_update().filter(token_hash=digest(raw)).first()
        if (approval is None or approval.used_at is not None or approval.expires_at <= timezone.now()
                or approval.session_hash != session_key(request) or str(approval.document_token) != str(token)
                or not secrets.compare_digest(approval.fingerprint, fingerprint)
                or not approval.approved_by.is_active or not is_admin(approval.approved_by)):
            raise ApprovalRequired(fingerprint)
        approval.used_at = timezone.now()
        approval.save(update_fields=['used_at'])
        return approval.approved_by
    return authorize


def audit(request, name, token, outcome):
    user = getattr(request, 'user', None)
    AuditEntry.objects.create(who=user if getattr(user, 'is_authenticated', False) else None,
                             actor_name=user.get_username() if getattr(user, 'is_authenticated', False) else '',
                             action='register.discount_override', target=str(token)[:220],
                             details={'manager': name[:150], 'outcome': outcome})


def _rate_lock_rows(cache, key):
    from shop_settings.models import RegisterOverrideCache
    return RegisterOverrideCache.objects.using(router.db_for_write(cache.cache_model_class)).filter(
        cache_key=cache.make_and_validate_key(key))


def _rate_lock_value(cache, owner):
    # Keep lock rows compatible with the existing DatabaseCache serialization.
    return base64.b64encode(pickle.dumps(owner, cache.pickle_protocol)).decode('latin1')


def _acquire_rate_lock(cache, key, owner):
    rows = _rate_lock_rows(cache, key)
    now = timezone.now()
    values = {'value': _rate_lock_value(cache, owner), 'expires': now + timedelta(seconds=30)}
    _, created = rows.get_or_create(defaults=values, cache_key=cache.make_and_validate_key(key))
    # DatabaseCache.add() unconditionally updates an expired row after reading it.
    # Recheck expiry in the UPDATE itself so only one contender can take ownership.
    return created or bool(rows.filter(expires__lt=now).update(**values))


def _release_rate_lock(cache, key, owner):
    # Compare ownership in the DELETE; a preceding cache.get() can race a takeover.
    # ORM delete signals can turn a filtered delete into SELECT then DELETE by PK.
    connection = connections[router.db_for_write(cache.cache_model_class)]
    table = connection.ops.quote_name(cache._table)
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM {table} WHERE cache_key = %s AND value = %s',
                       [cache.make_and_validate_key(key), _rate_lock_value(cache, owner)])


@never_cache
@require_POST
def manager_override(request, kind, token):
    name = ''
    if kind not in ('sale', 'order'):
        audit(request, name, token, 'invalid')
        return JsonResponse({'error': 'Invalid document type.'}, status=400)
    if not getattr(request, 'user', None) or not request.user.is_active or not is_register(request.user):
        audit(request, name, token, 'denied')
        return JsonResponse({'error': 'A register login is required.'}, status=403)
    try:
        if len(request.body) > 220000: raise ValueError
        body = json.loads(request.body)
        name, password, data = body['username'], body['password'], body['document']
        if not isinstance(name, str) or not isinstance(password, str) or not isinstance(data, dict): raise ValueError
        if not name or len(name) > 150 or len(password) > 1024: raise ValueError
        if str(data.get('token')) != str(token): raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        audit(request, '', token, 'invalid')
        return JsonResponse({'error': 'Invalid approval request.'}, status=400)
    cache = caches['register_overrides']
    keys = ['override:session:' + session_key(request), 'override:admin:' + digest(name.casefold())]
    held = []
    lock_owner = secrets.token_urlsafe(24)
    try:
        for key in sorted(keys):
            if not _acquire_rate_lock(cache, key + ':lock', lock_owner):
                audit(request, name, token, 'busy')
                return JsonResponse({'error': 'Please wait before trying again.'}, status=429)
            held.append(key + ':lock')
        now = timezone.now().timestamp()
        # Keep the actual rolling window; refreshing a bare counter's TTL would count
        # failures older than fifteen minutes when attempts are spaced apart.
        failures = {key: [stamp for stamp in cache.get(key, []) if stamp > now - 900] for key in keys}
        if any(len(stamps) >= 5 for stamps in failures.values()):
            audit(request, name, token, 'rate_limited')
            return JsonResponse({'error': 'Too many failed approvals. Try again in 15 minutes.'}, status=429)
        manager = authenticate(request, username=name, password=password)
        if manager is None or not manager.is_active or not is_admin(manager):
            failed_at = timezone.now().timestamp()
            for key in keys:
                cache.set(key, [*failures[key], failed_at], 900)
            audit(request, name, token, 'refused')
            return JsonResponse({'error': 'Manager credentials were refused.'}, status=403)
        try:
            # Always roll back a probe, even when no approval is needed. Never post a sale from this endpoint.
            from django.db import transaction
            with transaction.atomic():
                save_document(data, kind, token, discount_authorizer=authorizer(request, data, kind, token, probe=True),
                              can_edit_items=register_can(request, 'edit_items'))
                transaction.set_rollback(True)
        except ApprovalRequired as challenge:
            raw = secrets.token_urlsafe(32)
            DiscountApproval.objects.create(token_hash=digest(raw), session_hash=session_key(request), document_token=token,
                                            fingerprint=challenge.fingerprint, approved_by=manager,
                                            expires_at=timezone.now() + timedelta(minutes=2))
            audit(request, name, token, 'approved')
            return JsonResponse({'token': raw})
        except PermissionDenied as exc:
            audit(request, name, token, 'denied_document')
            return JsonResponse({'error': str(exc)}, status=403)
        except (WorkflowError, ValueError) as exc:
            audit(request, name, token, 'invalid_document')
            return JsonResponse({'error': str(exc)}, status=400)
        audit(request, name, token, 'unneeded')
        return JsonResponse({'error': 'This document does not need manager approval.'}, status=400)
    finally:
        for key in held:
            # An expired lock may already belong to a newer request. Never release its lock.
            _release_rate_lock(cache, key, lock_owner)
