# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compatibility run: explicit saved reference policy, without editing existing tests."""
from .test_settings import *  # noqa: F403,F401
REGISTER_RULES_TEST_DEFAULTS = {
    'require_customer': True, 'require_discount_reason': True,
    'edit_prices': True, 'edit_costs': True, 'see_costs': True, 'adjust_stock': True,
    'edit_items': True, 'edit_vendors': True, 'edit_departments': True,
    'manage_charge_accounts': True, 'receipt_discounts': True,
    'discount_override_enabled': False, 'discount_override_percent': 0,
}
