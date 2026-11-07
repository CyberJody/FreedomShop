# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template context processor: {{ business }} (Business Settings) and {{ shop_tax }} (the tax setup) on every page."""
from .config import business, tax


def context(request):
    from .permissions import register_can
    from .models import REGISTER_RIGHTS
    from .config import register_rules
    return {'business': business(), 'shop_tax': tax(), 'register_rules': register_rules(),
            'register_rights': {right: register_can(request, right) for right in REGISTER_RIGHTS}}
