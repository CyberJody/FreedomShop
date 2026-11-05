# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allowlisted server sorting shared by list screens."""

def sorted_list(request, queryset, fields, default):
    allowed={field for field in fields if field}
    sort=request.GET.get('sort',default)
    if sort.lstrip('-') not in allowed:
        sort=default
    return queryset.order_by(sort,'pk'), {'sort':sort,'sort_fields':fields}
