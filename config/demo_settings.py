# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local SQLite demo copy; never imports deployment settings or connects to PostgreSQL.

Use --settings=config.demo_settings for migrate, setup, load_demo and runserver.
Override FREEDOMSHOP_DEMO_DB / FREEDOMSHOP_DEMO_MEDIA to keep multiple copies.
"""
import os
from pathlib import Path

from .test_settings import *  # noqa: F403

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                         'NAME': os.environ.get('FREEDOMSHOP_DEMO_DB', '/tmp/freedomshop-demo.sqlite3')}}
MEDIA_ROOT = os.environ.get('FREEDOMSHOP_DEMO_MEDIA', '/tmp/freedomshop-demo-media')
SHOP_SETTINGS_SEED = Path(__file__).resolve().parent.parent / 'shop_settings/seeds/default.json'
MIDDLEWARE = ['django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware',
              'shop_settings.middleware.SetupGateMiddleware',
              'accounts.middleware.RequireLoginMiddleware']
POS_REQUIRE_LOGIN = True
PRINT_LOGO_FILE = ''
