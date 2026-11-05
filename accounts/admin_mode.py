# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Mode = signed in as an admin.

There is no switch any more: the File-menu toggle and POST /admin-mode/ are gone. Admin screens (Reports, Server
Status, Admin Override of a past receipt) open only for an active member of the Admin group. The shared register
login (`register`) and PIN cashiers never get them; `is_staff` / `is_superuser` never count. Views that belong to
Admin Mode use @admin_mode_required, which sends everyone else Home with a notice to sign in as an admin.
"""
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import redirect

REFUSED = 'Sign in as an admin to use this.'


def is_admin(request):
    # Group membership only: is_staff / is_superuser never count.
    from .roles import is_admin as admin_user
    user = getattr(request, 'user', None)
    return bool(user is not None and user.is_active and admin_user(user))


def is_on(request):
    """Admin Mode is on exactly when the signed-in user is an active admin."""
    return is_admin(request)


def context(request):
    """Template context processor: {{ admin_mode }} on every page."""
    return {'admin_mode': is_on(request)}


def admin_mode_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_on(request):
            if request.method == 'POST':
                return JsonResponse({'error': REFUSED}, status=403)
            return redirect('/?admin=off')
        return view(request, *args, **kwargs)
    return wrapped


@admin_mode_required
def server_status_view(request):
    from django.shortcuts import render
    from . import server_status
    sections, state, summary, gathered_at = server_status.cached(refresh='refresh' in request.GET)
    response = render(request, 'server-status.html', {'title': 'Server Status', 'sections': sections, 'state': state,
                                                      'summary': summary, 'gathered_at': gathered_at})
    response['Cache-Control'] = 'no-store'
    return response
