# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class ShopSettingsConfig(AppConfig):
    name = 'shop_settings'
    verbose_name = 'Shop settings'

    def ready(self):
        from . import signals  # noqa: F401
