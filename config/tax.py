# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep an item's or department's tax category and rate coherent, using the shop's Tax Settings categories."""
from decimal import Decimal

from shop_settings.config import NON_KEYS, TAXABLE_KEYS, category_key, tax


def coherent_tax(category, rate, default=None):
    """(category name, rate): a non-taxable category always means 0 %; a blank or generic taxable name follows the rate
    (0 % -> the non-taxable category, else the shop's taxable one); other names and explicit rates are kept.
    A blank rate takes the category default, then `default` (the department rate), then the shop default."""
    config = tax()
    category = category or ''
    exact = next((c for c in config.categories if c.name == category), None)
    category = category.strip()
    key = category_key(category)
    named = exact or (next((c for c in config.categories if category_key(c.name) == key), None) if key else None)
    if key in NON_KEYS or (named is not None and not named.taxable):
        return (named or config.nontaxable_category).name, Decimal('0')
    if rate is None:
        rate = (named.default_rate if named is not None and named.default_rate is not None
                else config.line_rate if default is None else default)
        if not config.charge_tax:
            rate = Decimal('0')
    if key in TAXABLE_KEYS or (named is not None and named.name == config.taxable_category.name):
        category = config.nontaxable_category.name if rate == 0 else (named.name if named else config.taxable_category.name)
    elif named is not None:
        category = named.name
    return category, rate
