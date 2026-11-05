# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep the settings cache honest: drop it when a settings row changes and at the start of every request, and make
sure a tax setup exists after `migrate` (and after a test flush, which re-sends post_migrate)."""
from django.apps import apps
from django.core.signals import request_started
from django.db.models.signals import post_delete, post_migrate, post_save

from .config import UNREADABLE, clear_cache, ensure_configured


def _configure(sender, using='default', **kwargs):
    try:
        ensure_configured(using=using)
    except UNREADABLE:   # the settings tables aren't there yet (a partial migrate)
        pass


request_started.connect(clear_cache, dispatch_uid='shop_settings_request')
for model in apps.get_app_config('shop_settings').get_models():
    post_save.connect(clear_cache, sender=model, dispatch_uid=f'shop_settings_save_{model.__name__}')
    post_delete.connect(clear_cache, sender=model, dispatch_uid=f'shop_settings_delete_{model.__name__}')
post_migrate.connect(_configure, sender=apps.get_app_config('shop_settings'), dispatch_uid='shop_settings_configure')
