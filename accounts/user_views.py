# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-only, CSRF-protected forms. POSTs redirect with non-secret notices only."""
from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_safe

from .models import AuditEntry, Cashier
from .roles import ADMIN_GROUP, REGISTER_GROUP
from .user_forms import (AddCashierForm, AddLoginForm, CashierNameForm, ConfirmForm, PinForm,
                         RenameLoginForm, RoleForm, require_reauthentication)
from .user_management import (admin_login, audit, cashier_in_use, cashier_snapshot, guard_login,
                              login_snapshot, verify_admin_password)

LOGIN_ACTIONS = {'add': 'Add login', 'rename': 'Rename login', 'disable': 'Disable login',
                 'enable': 'Enable login', 'password': 'Reset password', 'role': 'Change role', 'delete': 'Delete login'}
CASHIER_ACTIONS = {'add': 'Add cashier', 'rename': 'Rename cashier', 'pin': 'Reset PIN',
                   'remove': 'Remove cashier', 'disable': 'Disable cashier', 'enable': 'Enable cashier'}
NOTICE_KEY = 'users_management_notice'


def admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not admin_login(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def notice(request, text, error=False):
    # No POST data, credentials, bound forms or exception representations in sessions.
    request.session[NOTICE_KEY] = {'text': text, 'error': error}


def render_form(request, form, title, target=None, explanation=''):
    return render(request, 'settings/user-form.html', {
        'title': title, 'form': form, 'target': target, 'explanation': explanation,
        'notice': request.session.pop(NOTICE_KEY, None)})


def locked_actor(request):
    # Every login mutation takes this one durable lock before reading memberships,
    # including promotions and changes by actors demoted in another request.
    admin_group, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
    Group.objects.select_for_update().get(pk=admin_group.pk)
    actor = get_user_model().objects.select_for_update().get(pk=request.user.pk)
    if not actor.is_active or not admin_login(actor):
        raise PermissionDenied
    return actor


def managed_logins():
    User = get_user_model()
    return User.objects.filter(pk__in=User.objects.filter(
        groups__name__in=[ADMIN_GROUP, REGISTER_GROUP]).values('pk'))


@login_required
@admin_required
@never_cache
@require_safe
def users(request):
    logins = [{'user': user, 'role': 'Admin' if admin_login(user) else 'Register'}
              for user in managed_logins().order_by('username')]
    return render(request, 'settings/users.html', {
        'title': 'Users & Cashiers', 'logins': logins, 'cashiers': Cashier.objects.order_by('name'),
        'entries': AuditEntry.objects.filter(action__startswith='users.').select_related('who')[:50],
        'notice': request.session.pop(NOTICE_KEY, None)})


@sensitive_variables()
def login_form(action, target, data=None):
    if action == 'add':
        form = AddLoginForm(data, initial={'role': REGISTER_GROUP})
    elif action == 'rename':
        form = RenameLoginForm(data, user=target, initial={'username': target.username})
    elif action == 'role':
        form = RoleForm(data, initial={'role': ADMIN_GROUP if admin_login(target) else REGISTER_GROUP})
    elif action == 'password':
        form = SetPasswordForm(target, data)
    else:
        form = ConfirmForm(data)
    needs_password = bool(target and admin_login(target)) or (
        action in ('add', 'role') and data is not None and data.get('role') == ADMIN_GROUP)
    if needs_password or action in ('add', 'role'):
        require_reauthentication(form)
        form.fields['current_password'].required = needs_password
        form.fields['current_password'].help_text = 'Required for any change to an admin login, including promotion.'
    return form, needs_password


@login_required
@admin_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
@sensitive_post_parameters()
@sensitive_variables()
def login_change(request, action, pk=None):
    if action not in LOGIN_ACTIONS or (pk is None) != (action == 'add'):
        raise Http404
    target = get_object_or_404(managed_logins(), pk=pk) if pk is not None else None
    if request.method != 'POST':
        form, _ = login_form(action, target)
        return render_form(request, form, LOGIN_ACTIONS[action], target.get_username() if target else None,
                           'Deletion is permanent. Disable a login to keep it available for later.' if action == 'delete' else '')
    error = None
    try:
        with transaction.atomic():
            actor = locked_actor(request)
            if target:
                target = get_object_or_404(managed_logins().select_for_update(), pk=pk)
            form, needs_password = login_form(action, target, request.POST)
            if not form.is_valid():
                error = ' '.join(str(message) for errors in form.errors.values() for message in errors)
            else:
                try:
                    if target:
                        guard_login(actor, target, action, form.cleaned_data.get('role'))
                    if needs_password:
                        verify_admin_password(request, actor, form.cleaned_data['current_password'])
                except ValidationError as exc:
                    # Keep cache failures committed even when verification is refused.
                    error = ' '.join(exc.messages)
                else:
                    before = login_snapshot(target) if target else None
                    if action == 'add':
                        target = get_user_model()(username=form.cleaned_data['username'])
                        target.set_password(form.cleaned_data['password1'])
                        target.save()
                        target.groups.add(Group.objects.get_or_create(name=form.cleaned_data['role'])[0])
                    elif action == 'rename':
                        target.username = form.cleaned_data['username']
                        target.save(update_fields=['username'])
                    elif action in ('disable', 'enable'):
                        target.is_active = action == 'enable'
                        target.save(update_fields=['is_active'])
                    elif action == 'password':
                        form.save()
                    elif action == 'role':
                        target.groups.remove(*Group.objects.filter(name__in=[ADMIN_GROUP, REGISTER_GROUP]))
                        target.groups.add(Group.objects.get_or_create(name=form.cleaned_data['role'])[0])
                    target_label = f'login:{target.pk}'
                    after = login_snapshot(target)
                    if action == 'delete':
                        target.delete()
                        after = None
                    audit(request, actor, f'users.login.{action}', target_label, before, after)
    except IntegrityError:
        error = 'The login could not be saved. Its name may already be in use; reload and try again.'
    if error:
        notice(request, error, error=True)
        return redirect(request.path)
    notice(request, f'{LOGIN_ACTIONS[action]} completed.')
    return redirect('users-settings')


@login_required
@admin_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
@sensitive_post_parameters()
@sensitive_variables()
def cashier_change(request, action, pk=None):
    if action not in CASHIER_ACTIONS or (pk is None) != (action == 'add'):
        raise Http404
    cashier = get_object_or_404(Cashier, pk=pk) if pk is not None else None
    title = CASHIER_ACTIONS[action]
    if request.method != 'POST':
        if action == 'add':
            form = AddCashierForm()
        elif action == 'rename':
            form = CashierNameForm(cashier=cashier, initial={'name': cashier.name})
        elif action == 'pin':
            form = PinForm()
        else:
            form = ConfirmForm()
        explanation = ('A cashier named on sales, payments or pickup history will be disabled instead of deleted. '
                       'Earlier sales keep the name recorded at the time.') if action in ('remove', 'rename') else ''
        return render_form(request, form, title, cashier.name if cashier else None, explanation)
    error = None
    result_action = action
    try:
        with transaction.atomic():
            actor = locked_actor(request)
            if cashier:
                cashier = get_object_or_404(Cashier.objects.select_for_update(), pk=pk)
            if action == 'add':
                form = AddCashierForm(request.POST)
            elif action == 'rename':
                form = CashierNameForm(request.POST, cashier=cashier)
            elif action == 'pin':
                form = PinForm(request.POST)
            else:
                form = ConfirmForm(request.POST)
            if not form.is_valid():
                error = ' '.join(str(message) for errors in form.errors.values() for message in errors)
            else:
                before = cashier_snapshot(cashier) if cashier else None
                if action == 'add':
                    cashier = Cashier.objects.create(name=form.cleaned_data['name'],
                                                      pin_hash=make_password(form.cleaned_data['pin']))
                elif action == 'rename':
                    if cashier.name != form.cleaned_data['name']:
                        cashier.previous_names = list(dict.fromkeys([*cashier.previous_names, cashier.name]))
                        cashier.name = form.cleaned_data['name']
                        cashier.save(update_fields=['name', 'previous_names'])
                elif action == 'pin':
                    cashier.pin_hash = make_password(form.cleaned_data['pin'])
                    cashier.save(update_fields=['pin_hash'])
                elif action in ('disable', 'enable', 'remove'):
                    if action == 'remove':
                        result_action = 'disable' if cashier_in_use(cashier) else 'remove'
                    if result_action != 'remove':
                        cashier.active = result_action == 'enable'
                        cashier.save(update_fields=['active'])
                target_label = f'cashier:{cashier.pk}'
                after = cashier_snapshot(cashier)
                if result_action == 'remove':
                    cashier.delete()
                    after = None
                audit(request, actor, f'users.cashier.{result_action}', target_label, before, after)
    except IntegrityError:
        error = 'The cashier could not be saved. Their name may already be in use; reload and try again.'
    if error:
        notice(request, error, error=True)
        return redirect(request.path)
    notice(request, 'Cashier has history and was disabled.' if action == 'remove' and result_action == 'disable'
           else f'{title} completed.')
    return redirect('users-settings')
