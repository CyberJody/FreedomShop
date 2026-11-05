# SPDX-License-Identifier: AGPL-3.0-or-later
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

class RequireLoginMiddleware:
    def __init__(self,get_response): self.get_response=get_response
    def __call__(self,request):
        protected=getattr(settings,'POS_REQUIRE_LOGIN',True) and request.path not in {'/login/','/health/','/about/','/about/license/'} and not request.path.startswith('/setup/')  # setup views check access themselves
        if protected and not request.user.is_authenticated:
            if request.headers.get('Content-Type','').startswith('application/json') or request.headers.get('Accept')=='application/json':
                return JsonResponse({'error':'Your session ended. Sign in again before continuing.','login_url':'/login/'},status=401)
            return redirect_to_login(request.get_full_path(),settings.LOGIN_URL)
        response=self.get_response(request)
        if protected:
            response['Cache-Control']='no-store, private'
        return response
