# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
from datetime import timedelta
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from .models import LoginWindow
from .roles import login_choices

class TestingLoginForm(AuthenticationForm):
    """Sign-in screen: the username is a dropdown of the admins and the shared register login only.

    PIN staff are not Django users and are never listed; a username posted directly that is not in the list is
    refused before any password check, so nothing but an admin or the register login can sign in here."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['username']=forms.ChoiceField(label='User',choices=[('','Choose your name…')]+login_choices(),
            error_messages={'required':'Choose your name from the list.','invalid_choice':'Choose your name from the list.'})

    def clean(self):
        username=self.cleaned_data.get('username','').strip().casefold()
        if not username: return self.cleaned_data  # not in the list: no password check, no throttle hit
        key=hashlib.sha256(username.encode()).hexdigest()
        error=None;result=None
        # The database makes the limit shared across application workers.
        with transaction.atomic():
            window,_=LoginWindow.objects.get_or_create(key=key)
            window=LoginWindow.objects.select_for_update().get(pk=window.pk)
            if window.started_at<timezone.now()-timedelta(minutes=15):
                window.failures=0;window.started_at=timezone.now()
            if window.failures>=10:
                error=forms.ValidationError('Too many unsuccessful attempts. Please try again in 15 minutes.')
            else:
                try: result=super().clean()
                except forms.ValidationError as exc:
                    window.failures+=1;error=exc
                else: window.failures=0
                window.save(update_fields=['failures','started_at'])
        if error: raise error
        return result

@method_decorator(never_cache,name='dispatch')
class TestingLoginView(LoginView):
    template_name='login.html'
    authentication_form=TestingLoginForm
    redirect_authenticated_user=True

class TestingLogoutView(LogoutView):
    next_page='/login/'
