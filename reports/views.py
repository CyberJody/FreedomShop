# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
from functools import wraps

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from accounts.admin_mode import admin_mode_required
from purchasing.models import Vendor
from sales.printing import PrintError

from . import direct_print, exports, sales_by_vendor

FORMATS = {
    'xlsx': (exports.xlsx_bytes, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    'pdf': (exports.pdf_bytes, 'application/pdf'),
}
PRINTER_SESSION_KEY = 'report_printer'


@admin_mode_required
@require_safe
def report_index(request):
    return render(request, 'reports/index.html', {'title': 'Reports'})


@admin_mode_required
@never_cache
@require_safe
def exchange_view(request):
    from django.utils import timezone
    from . import exchange
    start = request.GET.get('start', timezone.localdate().isoformat())
    end = request.GET.get('end') or start
    try:
        days = list(exchange.days_between(start, end))
    except (ValueError, TypeError) as exc:
        return render(request, 'reports/exchange.html', dict(title='FreedomBooks exchange', start=start, end=end,
                                                            error=str(exc)), status=400)
    if request.GET.get('format') == 'zip':
        try:
            data = exchange.zip_bytes(start, end)
        except (ValueError, OSError) as exc:
            return HttpResponse(str(exc), status=400, content_type='text/plain')
        response = HttpResponse(data, content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="freedomshop-exchange-{start}-{end}.zip"'
        return response
    if request.GET.get('format'):
        raise Http404
    reports = []
    for day in days:
        export = exchange.build_day(day)
        body = export['body']
        reports.append(dict(body=body, command_json=json.dumps(body['commands'], ensure_ascii=False, sort_keys=True, indent=2),
                            diagnostic_json=json.dumps(body['diagnostics'], sort_keys=True, indent=2),
                            digest=export['body_sha256'], checks=body['checks'].items(),
                            summary=[(key.replace('_', ' ').capitalize(), value) for key, value in body['summary'].items()
                                     if isinstance(value, str)]))
    from .models import ExchangeRun
    return render(request, 'reports/exchange.html', dict(title='FreedomBooks exchange', start=start, end=end, reports=reports,
                                                        runs=ExchangeRun.objects.all()[:100]))


@admin_mode_required
@never_cache
@require_safe
def exchange_download(request, day):
    from .exchange import parse_day
    try:
        parse_day(day)
    except (ValueError, TypeError):
        raise Http404
    return _exchange_attachment(day)


def _exchange_attachment(day):
    from .exchange_sync import export_day, outbox_path
    from shop_settings.config import business
    try:
        run = export_day(day, outbox_path() if business().exchange_outbox else None)
    except (ValueError, TypeError, OSError) as exc:
        return HttpResponse(str(exc), status=400, content_type='text/plain')
    response = HttpResponse(bytes(run.payload), content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="freedomshop-exchange-{day}.json"'
    return response


@admin_mode_required
@never_cache
@require_POST
def exchange_reexport(request, day):
    from .models import ExchangeRun
    if not ExchangeRun.objects.filter(day=day, exported_at__isnull=False).exists():
        raise Http404
    return _exchange_attachment(day)


def _params(data):
    default_start, default_end = sales_by_vendor.default_range()
    start = sales_by_vendor.parse_date(data.get('start'), default_start)
    end = sales_by_vendor.parse_date(data.get('end'), default_end)
    if end < start:
        start, end = end, start
    vendor = None
    key = data.get('vendor', '')
    if key:
        if not key.isdigit() or len(key) > 18:
            raise Http404
        vendor = Vendor.objects.filter(pk=key).first()
        if vendor is None:
            raise Http404
    return start, end, vendor


def _remembered_printer(request, printers):
    session = getattr(request, 'session', None)
    chosen = str(session.get(PRINTER_SESSION_KEY, '')) if session is not None else ''
    keys = [str(p.pk) for p in printers]
    return chosen if chosen in keys else (keys[0] if keys else '')


def _vendor_view(data):
    view = data.get('view', 'items')
    return view if view in sales_by_vendor.VIEWS else 'items'


def _vendor_report(start, end, vendor, view):
    """(report, formats): the per-line detail uses the original renderers; summaries are column-driven."""
    if view == 'detail':
        return sales_by_vendor.build(start, end, vendor), FORMATS
    from . import tabular_exports
    return sales_by_vendor.summarize(start, end, vendor, view), {
        'xlsx': (tabular_exports.xlsx_bytes, FORMATS['xlsx'][1]), 'pdf': (tabular_exports.pdf_bytes, FORMATS['pdf'][1])}


@admin_mode_required
@require_safe
def sales_by_vendor_view(request):
    start, end, vendor = _params(request.GET)
    view = _vendor_view(request.GET)
    fmt = request.GET.get('format', '')
    if fmt:
        if fmt not in FORMATS:
            raise Http404
        report, formats = _vendor_report(start, end, vendor, view)
        render_bytes, content_type = formats[fmt]
        response = HttpResponse(render_bytes(report), content_type=content_type)
        stem = re.sub(r'[^A-Za-z0-9]+', '-', f"{report.title} {vendor.company if vendor else 'All'} {start} {end}").strip('-')
        response['Content-Disposition'] = f'attachment; filename="{stem}.{fmt}"'
        return response
    ran = 'run' in request.GET
    report = _vendor_report(start, end, vendor, view)[0] if ran else None
    query = request.GET.copy()
    query.pop('format', None)
    printers = list(direct_print.letter_printers())
    return render(request, 'reports/sales-by-vendor.html', {
        'title': 'Sales by Vendor', 'report': report, 'ran': ran, 'view': view, 'views': sales_by_vendor.VIEWS.items(),
        'start': start, 'end': end, 'vendor': vendor,
        'vendors': Vendor.objects.filter(active=True).order_by('company'),
        'params': query.urlencode(),
        'printers': printers, 'selected_printer': _remembered_printer(request, printers),
    })


@admin_mode_required
@never_cache
@require_POST
def sales_by_vendor_print(request):
    """Render the report PDF for the posted options and send it to the chosen Letter printer."""
    try:
        start, end, vendor = _params(request.POST)
    except Http404:
        return JsonResponse({'error': 'That vendor is not available.'}, status=404)
    key = request.POST.get('printer', '')
    printer = direct_print.letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    report, formats = _vendor_report(start, end, vendor, _vendor_view(request.POST))
    if not report.lines:
        return JsonResponse({'error': 'Nothing to print: the report has no item sales in this range.'}, status=400)
    try:
        message = direct_print.print_report(printer, formats['pdf'][0](report), f'{report.title} {report.subtitle}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


# ---- Sales Tax -------------------------------------------------------------------------------------------------

from datetime import date as _date  # noqa: E402

from . import tax_periods, sales_tax, sales_tax_exports  # noqa: E402

TAX_FORMATS = {
    'xlsx': (sales_tax_exports.xlsx_bytes, FORMATS['xlsx'][1]),
    'pdf': (sales_tax_exports.pdf_bytes, FORMATS['pdf'][1]),
}


def tax_year_required(view):
    @wraps(view)
    def checked(request, *args, **kwargs):
        from shop_settings.config import tax
        if tax().tax_year_start_month is None:
            if request.method == 'POST':
                return JsonResponse({'error': 'Choose the tax year start month in Tax Settings before running reports.'}, status=400)
            return redirect('/settings/tax/?required=tax-year')
        return view(request, *args, **kwargs)
    return checked


def _tax_period(data, default='month'):
    from django.utils import timezone
    today = timezone.localdate()
    kind = data.get('period', default)
    if kind not in {'today', 'yesterday', 'month', 'quarter', 'year', 'custom'}:
        kind = 'month'
    value = data.get(kind, '')
    start = sales_by_vendor.parse_date(data.get('start'), None)
    end = sales_by_vendor.parse_date(data.get('end'), None)
    return tax_periods.resolve(kind, value, today, start, end), today


@admin_mode_required
@require_safe
@tax_year_required
def sales_tax_view(request):
    period, today = _tax_period(request.GET)
    detailed = request.GET.get('detail') == 'detail'
    fmt = request.GET.get('format', '')
    if fmt:
        if fmt not in TAX_FORMATS:
            raise Http404
        report = sales_tax.build(period, detailed)
        render_bytes, content_type = TAX_FORMATS[fmt]
        response = HttpResponse(render_bytes(report), content_type=content_type)
        stem = re.sub(r'[^A-Za-z0-9]+', '-', f"Sales Tax {'Detail' if detailed else 'Summary'} {period.start} {period.end}").strip('-')
        response['Content-Disposition'] = f'attachment; filename="{stem}.{fmt}"'
        return response
    ran = 'run' in request.GET
    report = sales_tax.build(period, detailed) if ran else None
    query = request.GET.copy()
    query.pop('format', None)
    printers = list(direct_print.letter_printers())
    return render(request, 'reports/sales-tax.html', {
        'title': 'Sales Tax', 'report': report, 'ran': ran, 'period': period, 'detailed': detailed,
        'notes': sales_tax_exports._notes(report) if report else [],
        'months': tax_periods.month_choices(today), 'quarters': tax_periods.quarter_choices(today), 'years': tax_periods.year_choices(today),
        'selected_month': f'{period.start:%Y-%m}' if period.kind == 'month' else f'{today:%Y-%m}',
        'selected_quarter': '%d-%d' % tax_periods.quarter_of(period.start) if period.kind == 'quarter' else '%d-%d' % tax_periods.quarter_of(today),
        'selected_year': str(tax_periods.tax_year_of(period.start) if period.kind == 'year' else tax_periods.tax_year_of(today)),
        'custom_start': period.start if period.kind == 'custom' else today.replace(day=1), 'custom_end': period.end if period.kind == 'custom' else today,
        'params': query.urlencode(), 'printers': printers, 'selected_printer': _remembered_printer(request, printers),
    })


@admin_mode_required
@never_cache
@require_POST
@tax_year_required
def sales_tax_print(request):
    period, _ = _tax_period(request.POST)
    detailed = request.POST.get('detail') == 'detail'
    key = request.POST.get('printer', '')
    printer = direct_print.letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    report = sales_tax.build(period, detailed)
    if not report.receipts:
        return JsonResponse({'error': 'Nothing to print: no sales receipts in this period.'}, status=400)
    try:
        message = direct_print.print_report(printer, sales_tax_exports.pdf_bytes(report), f'{report.title} {report.subtitle}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


# ---- Daily Totals ---------------------------------------------------------------------------------------------

from . import daily_totals_exports  # noqa: E402

DAILY_FORMATS = {
    'xlsx': (daily_totals_exports.xlsx_bytes, FORMATS['xlsx'][1]),
    'pdf': (daily_totals_exports.pdf_bytes, FORMATS['pdf'][1]),
}


def _daily_report(period):
    report = sales_tax.build_daily(period)
    # Presentation only: the ledger calculation remains shared and unchanged.
    report.title = 'Daily Totals'
    return report


@admin_mode_required
@require_safe
@tax_year_required
def daily_totals_view(request):
    period, today = _tax_period(request.GET, default='today')
    fmt = request.GET.get('format', '')
    if fmt:
        if fmt not in DAILY_FORMATS:
            raise Http404
        report = _daily_report(period)
        render_bytes, content_type = DAILY_FORMATS[fmt]
        response = HttpResponse(render_bytes(report), content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="Daily-Totals-{period.start}-{period.end}.{fmt}"'
        return response
    ran = 'run' in request.GET
    report = _daily_report(period) if ran else None
    query = request.GET.copy()
    query.pop('format', None)
    printers = list(direct_print.letter_printers())
    return render(request, 'reports/daily-totals-page.html', {
        'title': 'Daily Totals', 'report': report, 'ran': ran, 'period': period,
        'months': tax_periods.month_choices(today), 'quarters': tax_periods.quarter_choices(today), 'years': tax_periods.year_choices(today),
        'selected_month': f'{period.start:%Y-%m}' if period.kind == 'month' else f'{today:%Y-%m}',
        'selected_quarter': '%d-%d' % tax_periods.quarter_of(period.start) if period.kind == 'quarter' else '%d-%d' % tax_periods.quarter_of(today),
        'selected_year': str(tax_periods.tax_year_of(period.start) if period.kind == 'year' else tax_periods.tax_year_of(today)),
        'custom_start': period.start if period.kind == 'custom' else today.replace(day=1), 'custom_end': period.end if period.kind == 'custom' else today,
        'params': query.urlencode(), 'printers': printers, 'selected_printer': _remembered_printer(request, printers),
    })


@admin_mode_required
@never_cache
@require_POST
@tax_year_required
def daily_totals_print(request):
    period, _ = _tax_period(request.POST, default='today')
    key = request.POST.get('printer', '')
    printer = direct_print.letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    report = _daily_report(period)
    if not report.lines:
        return JsonResponse({'error': 'Nothing to print: no document rows in this period.'}, status=400)
    try:
        message = direct_print.print_report(printer, daily_totals_exports.pdf_bytes(report), f'Daily Totals {period.label}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


# ---- Inventory On Hand -----------------------------------------------------------------------------------------

from . import inventory_exports, inventory_on_hand  # noqa: E402

INVENTORY_FORMATS = {
    'xlsx': (inventory_exports.xlsx_bytes, FORMATS['xlsx'][1]),
    'pdf': (inventory_exports.pdf_bytes, FORMATS['pdf'][1]),
}


@admin_mode_required
@require_safe
def inventory_on_hand_view(request):
    from django.utils import timezone
    include_zero = request.GET.get('zero') == 'all'
    fmt = request.GET.get('format', '')
    if fmt:
        if fmt not in INVENTORY_FORMATS:
            raise Http404
        report = inventory_on_hand.build(include_zero)
        render_bytes, content_type = INVENTORY_FORMATS[fmt]
        response = HttpResponse(render_bytes(report), content_type=content_type)
        stem = re.sub(r'[^A-Za-z0-9]+', '-', f"Inventory On Hand {timezone.localdate()}").strip('-')
        response['Content-Disposition'] = f'attachment; filename="{stem}.{fmt}"'
        return response
    ran = 'run' in request.GET
    report = inventory_on_hand.build(include_zero) if ran else None
    query = request.GET.copy()
    query.pop('format', None)
    printers = list(direct_print.letter_printers())
    return render(request, 'reports/inventory-on-hand.html', {
        'title': 'Inventory On Hand', 'report': report, 'ran': ran, 'include_zero': include_zero, 'today': timezone.localdate(),
        'params': query.urlencode(), 'printers': printers, 'selected_printer': _remembered_printer(request, printers),
    })


@admin_mode_required
@never_cache
@require_POST
def inventory_on_hand_print(request):
    include_zero = request.POST.get('zero') == 'all'
    key = request.POST.get('printer', '')
    printer = direct_print.letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    report = inventory_on_hand.build(include_zero)
    if not report.items:
        return JsonResponse({'error': 'Nothing to print: no inventory items.'}, status=400)
    try:
        message = direct_print.print_report(printer, inventory_exports.pdf_bytes(report), f'{report.title} {report.subtitle}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


# ---- General Sales ---------------------------------------------------------------------------------------------

from . import general_sales, tabular_exports  # noqa: E402

GENERAL_FORMATS = {
    'xlsx': (tabular_exports.xlsx_bytes, FORMATS['xlsx'][1]),
    'pdf': (tabular_exports.pdf_bytes, FORMATS['pdf'][1]),
}


def _general_options(data):
    period, today = _tax_period(data)
    optional = {c.key for c in general_sales.COLUMNS if c.optional}
    if 'run' in data or 'cols' in data or data.get('submitted'):
        chosen = {k for k in data.getlist('cols') if k in optional}
    else:
        chosen = set(general_sales.DEFAULT_OPTIONAL)
    group_by = data.get('group', 'none')
    if group_by not in general_sales.GROUPINGS:
        group_by = 'none'
    # The checkbox sends 'on' when ticked; a hidden 'off' input follows it so an unticked box is still expressed.
    discounts = data.getlist('discounts') if hasattr(data, 'getlist') else [data.get('discounts', 'on')]
    highlight = 'on' in discounts or not discounts
    return dict(period=period, today=today, optional_keys=chosen, group_by=group_by,
                highlight_discounts=highlight, include_noninventory=data.get('items', 'all') != 'inventory')


def _general_build(options):
    return general_sales.build(options['period'], options['optional_keys'], options['group_by'], options['highlight_discounts'], options['include_noninventory'])


@admin_mode_required
@require_safe
@tax_year_required
def general_sales_view(request):
    options = _general_options(request.GET)
    fmt = request.GET.get('format', '')
    if fmt:
        if fmt not in GENERAL_FORMATS:
            raise Http404
        report = _general_build(options)
        render_bytes, content_type = GENERAL_FORMATS[fmt]
        response = HttpResponse(render_bytes(report), content_type=content_type)
        stem = re.sub(r'[^A-Za-z0-9]+', '-', f"General Sales {options['period'].start} {options['period'].end}").strip('-')
        response['Content-Disposition'] = f'attachment; filename="{stem}.{fmt}"'
        return response
    ran = 'run' in request.GET
    report = _general_build(options) if ran else None
    query = request.GET.copy()
    query.pop('format', None)
    printers = list(direct_print.letter_printers())
    period, today = options['period'], options['today']
    return render(request, 'reports/general-sales.html', {
        'title': 'General Sales', 'report': report, 'ran': ran, 'period': period,
        'optional_columns': [c for c in general_sales.COLUMNS if c.optional], 'chosen': options['optional_keys'],
        'groupings': general_sales.GROUPINGS.items(), 'group_by': options['group_by'],
        'highlight_discounts': options['highlight_discounts'], 'include_noninventory': options['include_noninventory'],
        'months': tax_periods.month_choices(today), 'quarters': tax_periods.quarter_choices(today), 'years': tax_periods.year_choices(today),
        'selected_month': f'{period.start:%Y-%m}' if period.kind == 'month' else f'{today:%Y-%m}',
        'selected_quarter': '%d-%d' % tax_periods.quarter_of(period.start) if period.kind == 'quarter' else '%d-%d' % tax_periods.quarter_of(today),
        'selected_year': str(tax_periods.tax_year_of(period.start) if period.kind == 'year' else tax_periods.tax_year_of(today)),
        'custom_start': period.start if period.kind == 'custom' else today.replace(day=1), 'custom_end': period.end if period.kind == 'custom' else today,
        'params': query.urlencode(), 'printers': printers, 'selected_printer': _remembered_printer(request, printers),
    })


@admin_mode_required
@never_cache
@require_POST
@tax_year_required
def general_sales_print(request):
    options = _general_options(request.POST)
    key = request.POST.get('printer', '')
    printer = direct_print.letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    report = _general_build(options)
    if not report.lines:
        return JsonResponse({'error': 'Nothing to print: no sales in this period.'}, status=400)
    try:
        message = direct_print.print_report(printer, tabular_exports.pdf_bytes(report), f'{report.title} {report.subtitle}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


# ---- Customer Balances ------------------------------------------------------------------------------------------

from . import customer_balance  # noqa: E402

BALANCE_FORMATS = GENERAL_FORMATS


def _balance_options(data):
    return dict(show=data.get('show', 'owed'), sort=data.get('sort', 'balance'), charge_only=data.get('charge') == '1')


@admin_mode_required
@require_safe
def customer_balance_view(request):
    from django.utils import timezone
    options = _balance_options(request.GET)
    fmt = request.GET.get('format', '')
    if fmt:
        if fmt not in BALANCE_FORMATS:
            raise Http404
        report = customer_balance.build(**options)
        render_bytes, content_type = BALANCE_FORMATS[fmt]
        response = HttpResponse(render_bytes(report), content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="Customer-Balances-{timezone.localdate()}.{fmt}"'
        return response
    ran = True  # quick look: the list shows as soon as the report opens (owed, largest balance first)
    report = customer_balance.build(**options)
    query = request.GET.copy()
    query.pop('format', None)
    printers = list(direct_print.letter_printers())
    return render(request, 'reports/customer-balance.html', {
        'title': 'Customer Balances', 'report': report, 'ran': ran,
        'show': report.show if report else options['show'], 'sort': report.sort if report else options['sort'],
        'charge_only': options['charge_only'], 'show_choices': customer_balance.SHOW.items(),
        'sort_choices': customer_balance.SORTS.items(), 'params': query.urlencode(),
        'printers': printers, 'selected_printer': _remembered_printer(request, printers),
    })


@admin_mode_required
@never_cache
@require_POST
def customer_balance_print(request):
    key = request.POST.get('printer', '')
    printer = direct_print.letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    report = customer_balance.build(**_balance_options(request.POST))
    if not report.customers:
        return JsonResponse({'error': 'Nothing to print: no customers with a balance.'}, status=400)
    try:
        message = direct_print.print_report(printer, tabular_exports.pdf_bytes(report), f'{report.title} {report.subtitle}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})
