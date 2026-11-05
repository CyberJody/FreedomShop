# SPDX-License-Identifier: AGPL-3.0-or-later
"""Items Trash screens: the Delete Selected confirmation window, the Items Trash list and Restore.

Admin Mode only for delete and restore; the Trash list itself is readable by anyone, like the
Item List's old Inactive filter was. The rules live in sales.trash.
"""
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from accounts.admin_mode import admin_mode_required, is_on
from sales.models import Item
from sales import trash as trash_rules
from .item_views import search_items

PAGE = 100


def _who(request):
    """Who trashed / restored: the signed-in login plus the active PIN cashier when there is one (wording)."""
    from accounts.cashiers import active
    user = getattr(request, 'user', None)
    name = user.get_username() if user is not None and user.is_authenticated else 'Admin Mode (no login)'
    cashier = active(request)
    return f'{name} · {cashier.name}' if cashier and cashier.name != name else name


def _selected_pks(source):
    ids = source.getlist('item')
    if len(ids) > trash_rules.MAX_BATCH or any(not x.isdigit() or len(x) > 18 for x in ids):
        raise Http404
    return [int(x) for x in dict.fromkeys(ids)]


def _selection(request, source):
    """The items a Delete window is about: ?item=.. ids, or ?all=unused for every never-used active item."""
    if source.get('all') == 'unused':
        qs = trash_rules.never_used(Item.objects.filter(active=True))
        return list(qs.defer('photo', 'comments').order_by('name', 'pk')[:trash_rules.MAX_BATCH]), True
    pks = _selected_pks(source)
    return list(Item.objects.filter(pk__in=pks, active=True).defer('photo', 'comments').order_by('name', 'pk')), False


@never_cache
@admin_mode_required
@require_http_methods(['GET', 'HEAD', 'POST'])
def delete_items(request):
    """QB POS "Delete Items" window: the selection sorted into items with warnings and items with nothing attached."""
    source = request.POST if request.method == 'POST' else request.GET
    items, all_unused = _selection(request, source)
    if request.method == 'POST':
        if not items:
            return redirect('/purchasing/items/')
        entries = trash_rules.trash([i.pk for i in items], by=_who(request))
        return redirect(f'/purchasing/trash/?moved={len(entries)}')
    usage = trash_rules.usage(items)
    rows = [dict(item=i, **usage[i.pk]) for i in items]
    warned = [r for r in rows if r['warnings']]
    clean = [r for r in rows if not r['warnings']]
    return render(request, 'purchasing/items-delete.html', dict(title='Delete Items', items=items, warned=warned, clean=clean,
                                                                all_unused=all_unused, back=request.GET.get('back', '/purchasing/items/')))


@never_cache
@require_http_methods(['GET', 'HEAD'])
def trash_list(request):
    query = request.GET.get('q', '').strip()[:100]
    qs = Item.objects.filter(active=False).defer('photo', 'comments').select_related('trash_entry', 'department', 'primary_vendor')
    if query:
        qs = search_items(qs, query)
    qs = qs.order_by('name', 'pk')
    params = request.GET.copy()
    params.pop('page', None)
    params.pop('moved', None)
    params.pop('restored', None)
    page = Paginator(qs, PAGE).get_page(request.GET.get('page'))
    rows = [dict(item=item, entry=getattr(item, 'trash_entry', None)) for item in page]
    notice = ''
    for key, verb in (('moved', 'moved to the Items Trash'), ('restored', 'restored to the Item List')):
        raw = request.GET.get(key, '')
        if raw.isdigit() and len(raw) <= 6:
            n = int(raw)
            notice = f'{n} item{"s" if n != 1 else ""} {verb}.'
    return render(request, 'purchasing/items-trash.html', dict(title='Items Trash', page=page, rows=rows, query=query, params=params.urlencode(),
                                                               notice=notice, admin_mode_on=is_on(request)))


@never_cache
@admin_mode_required
@require_POST
def restore_items(request):
    pks = _selected_pks(request.POST)
    if not pks:
        return redirect('/purchasing/trash/')
    restored = trash_rules.restore(pks, by=_who(request))
    back = request.POST.get('back', '')
    if back.startswith('/purchasing/items/') and not back.startswith('//'):
        return redirect(back + ('&' if '?' in back else '?') + f'restored={len(restored)}')
    return redirect(f'/purchasing/trash/?restored={len(restored)}')
