# SPDX-License-Identifier: AGPL-3.0-or-later
"""Truck Receiving screens: the truck list, the scan screen and its JSON endpoints."""
import json
from functools import wraps

from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import display_name, is_admin, is_register
from sales.services import WorkflowError
from . import truck as service
from .models import PurchaseDocument, TruckSession


def allowed(user):
    """Same people who can open a Receiving Voucher: the admins and the shared register login."""
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or is_admin(user) or is_register(user)))


def truck_access(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not allowed(getattr(request, 'user', None)):
            if request.method == 'POST' or request.headers.get('Accept') == 'application/json':
                return JsonResponse({'error': 'Sign in to the store to use Truck Receiving.'}, status=403)
            return HttpResponseForbidden('Sign in to the store to use Truck Receiving.')
        return view(request, *args, **kwargs)
    return wrapped


def by_name(request):
    try:
        from accounts.cashiers import active
        cashier = active(request)
    except Exception:
        cashier = None
    return cashier.name if cashier else display_name(request.user)


def _session(token):
    return get_object_or_404(TruckSession, token=token)


def _payload(request):
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        data = request.POST.dict()
    return data


def state_data(request, session):
    vendors, removed = service.table(session)
    html = render_to_string('purchasing/truck-table.html', dict(session=session, vendors=vendors, removed=removed), request=request)
    return dict(version=session.version, status=session.status, totals=service.summary(session), html=html)


@never_cache
@require_safe
@truck_access
def truck_list(request):
    open_trucks, received = service.truck_list()
    return render(request, 'purchasing/truck-list.html', dict(title='Truck Receiving', open_trucks=open_trucks, received=received))


@never_cache
@require_safe
@truck_access
def truck_new(request):
    return render(request, 'purchasing/truck.html', dict(title='Truck Receiving', session=None, vendors=[], removed=[],
                                                          totals=dict(pieces=0, pos=0, vendors=0)))


@never_cache
@require_safe
@truck_access
def truck_detail(request, token):
    session = _session(token)
    vendors, removed = service.table(session)
    vouchers = list(PurchaseDocument.objects.filter(truck_scans__session=session).distinct().order_by('pk'))
    return render(request, 'purchasing/truck.html', dict(title=f'Truck Receiving · {session.number}', session=session, vendors=vendors, removed=removed,
                                                          totals=service.summary(session), vouchers=vouchers,
                                                          error=request.GET.get('error', '')[:300]))


@never_cache
@require_POST
@truck_access
def truck_scan(request, token=None):
    text = str(_payload(request).get('code', ''))[:2000]
    created = False
    if token is None:
        # The first scan on "Start a truck" creates the truck; a scan that isn't added doesn't leave an empty one behind.
        session = TruckSession.objects.create(created_by=request.user, created_by_name=by_name(request)[:100])
        created = True
    else:
        session = _session(token)
    result = service.scan(session, text, request.user, by_name(request))
    if created and result['kind'] != 'ok':
        session.delete()
        return JsonResponse(dict(result, version=0, totals=dict(pieces=0, pos=0, vendors=0)))
    session.refresh_from_db()
    data = dict(result, **state_data(request, session))
    if created:
        data.update(token=str(session.token), number=session.number, url=f'/purchasing/truck/{session.token}/')
    return JsonResponse(data)


@never_cache
@require_safe
@truck_access
def truck_state(request, token):
    session = _session(token)
    if request.GET.get('v') == str(session.version):
        return JsonResponse(dict(version=session.version, unchanged=True))
    return JsonResponse(state_data(request, session))


@never_cache
@require_POST
@truck_access
def truck_remove(request, token):
    session = _session(token)
    data = _payload(request)
    def number(key):
        value = str(data.get(key) or '')
        if not value:
            return None
        if not value.isdigit() or len(value) > 18:
            raise Http404
        return int(value)
    try:
        count = service.remove(session, str(data.get('reason', '')), scan_id=number('scan'), line_id=number('line'))
    except WorkflowError as exc:
        return JsonResponse(dict(error=str(exc)), status=400)
    session.refresh_from_db()
    return JsonResponse(dict(removed=count, **state_data(request, session)))


@never_cache
@require_POST
@truck_access
def truck_receive(request, token):
    session = _session(token)
    try:
        if str(request.POST.get('version', '')) != str(session.version):
            raise WorkflowError('Pieces were scanned or removed since this page was drawn. Check the list, then Receive Items again.')
        service.receive(session, request.user)
    except WorkflowError as exc:
        from urllib.parse import urlencode
        return redirect(f'/purchasing/truck/{session.token}/?' + urlencode({'error': str(exc)}))
    return redirect(f'/purchasing/truck/{session.token}/?received=1')


@never_cache
@require_POST
@truck_access
def truck_cancel(request, token):
    session = _session(token)
    try:
        service.cancel(session)
    except WorkflowError as exc:
        from urllib.parse import urlencode
        return redirect(f'/purchasing/truck/{session.token}/?' + urlencode({'error': str(exc)}))
    return redirect('/purchasing/truck/')
