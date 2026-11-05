# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sales tax reporting periods for a tax year that starts on the first of any month (Tax Settings).

A tax year is named for the calendar year in which it ends, and its quarters are counted from its first month. With
a January start the tax year is the calendar year (Q1 = Jan–Mar). Worked example — start month 3
(`reports/ny_periods.py`): the year runs March 1 through the last day of February (March 1, 2025 – February 28, 2026
is sales tax year 2026); quarters 1 = Mar–May, 2 = Jun–Aug, 3 = Sep–Nov, 4 = Dec–Feb (NYS Tax Bulletin TB-ST-275).

Every function takes `start_month`; None means the shop's setting.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

QUARTERS = (1, 2, 3, 4)


def configured_start_month():
    from django.core.exceptions import ImproperlyConfigured
    from shop_settings.config import tax
    month = tax().tax_year_start_month
    if month is None:
        raise ImproperlyConfigured('Choose the tax year start month in Tax Settings before running reports or importing history.')
    return month


def _start(start_month):
    return configured_start_month() if start_month is None else start_month


def _add_months(year, month, months):
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def month_range(year, month):
    return date(year, month, 1), month_end(year, month)


def quarter_months(quarter, start_month=None):
    """(first, last) calendar month of a quarter."""
    first = _add_months(2000, _start(start_month), 3 * (quarter - 1))[1]
    return first, _add_months(2000, first, 2)[1]


def quarter_label_months(quarter, start_month=None):
    first, last = quarter_months(quarter, start_month)
    return f'{calendar.month_abbr[first]} – {calendar.month_abbr[last]}'


def tax_year_of(day, start_month=None):
    """Sales tax year containing a calendar date."""
    start = _start(start_month)
    return day.year + 1 if start != 1 and day.month >= start else day.year


def tax_year_range(tax_year, start_month=None):
    start = _start(start_month)
    first_year = tax_year if start == 1 else tax_year - 1
    end_year, end_month = _add_months(first_year, start, 11)
    return date(first_year, start, 1), month_end(end_year, end_month)


def quarter_of(day, start_month=None):
    """(tax_year, quarter) containing a calendar date."""
    start = _start(start_month)
    return tax_year_of(day, start), (day.month - start) % 12 // 3 + 1


def quarter_range(tax_year, quarter, start_month=None):
    start = _start(start_month)
    first = tax_year_range(tax_year, start)[0]
    year, month = _add_months(first.year, first.month, 3 * (quarter - 1))
    end_year, end_month = _add_months(year, month, 2)
    return date(year, month, 1), month_end(end_year, end_month)


def quarter_label(tax_year, quarter, start_month=None):
    start, end = quarter_range(tax_year, quarter, start_month)
    return f'Q{quarter} {tax_year} ({quarter_label_months(quarter, start_month)}) · {start:%-m/%-d/%Y} – {end:%-m/%-d/%Y}'


def tax_year_label(tax_year, start_month=None):
    start, end = tax_year_range(tax_year, start_month)
    return f'Sales tax year {tax_year} · {start:%-m/%-d/%Y} – {end:%-m/%-d/%Y}'


@dataclass
class Period:
    kind: str          # today | yesterday | month | quarter | year | custom
    start: date
    end: date
    label: str


DAY_KINDS = {'today': ('Today', 0), 'yesterday': ('Yesterday', 1)}


def day_period(kind, today):
    """A single-day Period relative to `today` (kind 'today' or 'yesterday')."""
    name, back = DAY_KINDS[kind]
    day = today - timedelta(days=back)
    return Period(kind, day, day, f'{name} · {day:%-m/%-d/%Y}')


def month_choices(today, count=24):
    """Most recent months first, as (value 'YYYY-MM', label)."""
    y, m = today.year, today.month
    out = []
    for _ in range(count):
        out.append((f'{y}-{m:02d}', f'{calendar.month_name[m]} {y}'))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def quarter_choices(today, count=12, start_month=None):
    start = _start(start_month)
    ty, q = quarter_of(today, start)
    out = []
    for _ in range(count):
        out.append((f'{ty}-{q}', quarter_label(ty, q, start)))
        q -= 1
        if q == 0:
            ty, q = ty - 1, 4
    return out


def year_choices(today, count=8, start_month=None):
    start = _start(start_month)
    ty = tax_year_of(today, start)
    return [(str(ty - i), tax_year_label(ty - i, start)) for i in range(count)]


def resolve(kind, value, today, custom_start=None, custom_end=None, start_month=None):
    """Turn form inputs into a Period. Bad input falls back to the current period of that kind."""
    if kind in DAY_KINDS:
        return day_period(kind, today)
    start = _start(start_month) if kind in ('quarter', 'year') else start_month
    try:
        if kind == 'quarter':
            ty, q = (int(x) for x in value.split('-'))
            if q not in QUARTERS:
                raise ValueError
            first, last = quarter_range(ty, q, start)
            return Period('quarter', first, last, quarter_label(ty, q, start))
        if kind == 'year':
            ty = int(value)
            first, last = tax_year_range(ty, start)
            return Period('year', first, last, tax_year_label(ty, start))
        if kind == 'custom':
            if custom_start is None or custom_end is None:
                raise ValueError
            if custom_end < custom_start:
                custom_start, custom_end = custom_end, custom_start
            return Period('custom', custom_start, custom_end, f'{custom_start:%-m/%-d/%Y} – {custom_end:%-m/%-d/%Y}')
        y, m = (int(x) for x in value.split('-'))
        if not 1 <= m <= 12:
            raise ValueError
        first, last = month_range(y, m)
        return Period('month', first, last, f'{calendar.month_name[m]} {y}')
    except (TypeError, ValueError, AttributeError):
        if kind == 'quarter':
            ty, q = quarter_of(today, start)
            first, last = quarter_range(ty, q, start)
            return Period('quarter', first, last, quarter_label(ty, q, start))
        if kind == 'year':
            ty = tax_year_of(today, start)
            first, last = tax_year_range(ty, start)
            return Period('year', first, last, tax_year_label(ty, start))
        first, last = month_range(today.year, today.month)
        return Period('month', first, last, f'{calendar.month_name[today.month]} {today.year}')
