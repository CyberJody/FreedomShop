# SPDX-License-Identifier: AGPL-3.0-or-later
"""Legacy March-start reporting periods, retained for import compatibility.

The sales tax year runs March 1 through the last day of February and is named for the calendar year in which it
ends (March 1, 2025 – February 28, 2026 is sales tax year 2026). Quarters: 1 = Mar–May, 2 = Jun–Aug, 3 = Sep–Nov,
4 = Dec–Feb. Sources: NYS Tax Bulletin TB-ST-275 (Filing Requirements for Sales and Use Tax Returns) and
"Filing Period Indicators on Final Sales Tax Returns", tax.ny.gov.

The reports use `reports.tax_periods` with the shop's own start month (Tax Settings); this module is the fixed New
York case, kept as the worked example.
"""
from functools import partial

from . import tax_periods
from .tax_periods import DAY_KINDS, Period, day_period, month_choices, month_end, month_range  # noqa: F401

START_MONTH = 3
QUARTER_STARTS = {1: 3, 2: 6, 3: 9, 4: 12}   # quarter -> first month
QUARTER_LABELS = {1: 'Mar – May', 2: 'Jun – Aug', 3: 'Sep – Nov', 4: 'Dec – Feb'}

tax_year_of = partial(tax_periods.tax_year_of, start_month=START_MONTH)
tax_year_range = partial(tax_periods.tax_year_range, start_month=START_MONTH)
quarter_of = partial(tax_periods.quarter_of, start_month=START_MONTH)
quarter_range = partial(tax_periods.quarter_range, start_month=START_MONTH)
quarter_label = partial(tax_periods.quarter_label, start_month=START_MONTH)
tax_year_label = partial(tax_periods.tax_year_label, start_month=START_MONTH)
quarter_choices = partial(tax_periods.quarter_choices, start_month=START_MONTH)
year_choices = partial(tax_periods.year_choices, start_month=START_MONTH)
resolve = partial(tax_periods.resolve, start_month=START_MONTH)
