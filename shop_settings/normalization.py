# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable identities for stored location aliases; also used by data migrations."""


def location_key(name):
    return (name or '').strip().casefold()


def references_name(queryset, field, name, normalize=location_key):
    key = normalize(name)
    return any(normalize(value) == key for value in queryset.values_list(field, flat=True).distinct())
