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
