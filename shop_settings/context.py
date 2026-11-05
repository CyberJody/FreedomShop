# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template context processor: {{ business }} (Business Settings) and {{ shop_tax }} (the tax setup) on every page."""
from .config import business, tax


def context(request):
    return {'business': business(), 'shop_tax': tax()}
