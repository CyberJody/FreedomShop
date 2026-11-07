# SPDX-License-Identifier: AGPL-3.0-or-later
"""Until first-run setup is finished (Business.setup_complete), every page goes to the setup wizard at /setup/.
Setup itself, sign-in/out, the health check and static files stay reachable. Put it after AuthenticationMiddleware and
before RequireLoginMiddleware, so a brand-new install opens on setup rather than on an empty sign-in list."""
from django.http import JsonResponse
from django.shortcuts import redirect

from .setup import setup_done

OPEN_PATHS = ('/setup/', '/health/', '/static/', '/login/', '/logout/', '/about/')


class SetupGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(OPEN_PATHS) and not setup_done():
            if request.headers.get('Content-Type', '').startswith('application/json') or request.headers.get('Accept') == 'application/json':
                return JsonResponse({'error': 'Finish setting up the shop first.', 'setup_url': '/setup/'}, status=503)
            return redirect('/setup/')
        return self.get_response(request)


class BackupMaintenanceMiddleware:
    """First middleware: no sessions/database work runs while a restore is switching.

    Shared flock drains in-flight requests before a switch. The switch endpoint
    takes its own operation lock and must not hold the shared lock while switching.
    """
    def __init__(self, get_response):
        from threading import local
        self.get_response = get_response
        self.state = local()

    def __call__(self, request):
        from django.db import connections
        from django.http import HttpResponse
        from django.template.loader import render_to_string
        from .backups import control_path, file_lock
        from .config import clear_cache

        def maintenance():
            # A request context would run business/session context processors.
            response = HttpResponse(render_to_string('settings/maintenance.html'), status=503)
            response['Retry-After'] = '60'
            response['Cache-Control'] = 'no-store'
            return response

        if control_path('MAINTENANCE').exists():
            return maintenance()

        def refresh():
            generation_file = control_path('GENERATION')
            generation = generation_file.read_text() if generation_file.exists() else ''
            if generation != getattr(self.state, 'generation', None):
                connections.close_all()
                clear_cache()
                self.state.generation = generation

        if (request.method == 'POST' and request.path.startswith('/settings/backups/')
                and request.path.endswith('/switch/')):
            refresh()
            return self.get_response(request)
        with file_lock('.requests', shared=True, blocking=True):
            if control_path('MAINTENANCE').exists():
                return maintenance()
            refresh()
            return self.get_response(request)
