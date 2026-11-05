# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reorder Reminders screen (QB POS Inventory > Reminders > Reorder Items)."""
from urllib.parse import urlencode

from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from sales.models import Item
from . import reorder


def _rule(request):
    rule = request.GET.get('rule', request.POST.get('rule', reorder.DEFAULT_RULE))
    return rule if rule in reorder.RULES else reorder.DEFAULT_RULE


def _po_url(group):
    params = [('item', row['item'].pk) for row in group['rows']]
    vendor = group['vendor']
    if vendor and vendor.active:
        params.insert(0, ('vendor', vendor.pk))
    return '/purchasing/po/new/?' + urlencode(params)


@never_cache
@require_safe
def reminder_list(request):
    rule = _rule(request)
    reorder.clear_restocked(rule)
    listed, ignored = reorder.reminders(rule)
    show = 'all' if request.GET.get('show') == 'all' else 'below'
    shown = listed
    if show == 'all':  # every item with a reorder point, short or not (to check the points that are set)
        shown = sorted((r for r in reorder.evaluate(rule) if not (r['below'] and r['ignored'])), key=reorder.sort_key)
    groups = reorder.by_vendor(shown)
    for group in groups:
        group['po_url'] = _po_url(dict(group, rows=[r for r in group['rows'] if r['below']]))
        group['short'] = sum(1 for r in group['rows'] if r['below'])
    return render(request, 'purchasing/reorder.html', dict(
        title='Reorder Reminders', rule=rule, rules=reorder.RULES, show=show, groups=groups, ignored=ignored, count=len(listed)))


@require_POST
def reminder_ignore(request, pk):
    item = get_object_or_404(Item, pk=pk)
    reorder.set_ignored(item, request.POST.get('ignore') == '1')
    params = {}
    if _rule(request) != reorder.DEFAULT_RULE:
        params['rule'] = _rule(request)
    if request.POST.get('show') == 'all':
        params['show'] = 'all'
    return redirect('/purchasing/reorder/' + (f'?{urlencode(params)}' if params else '') + f'#item-{item.pk}')
