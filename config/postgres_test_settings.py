# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated PostgreSQL tests: local peer auth, or explicit CI connection values."""
import os

from .test_settings import *
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.postgresql', 'NAME': 'postgres',
    'USER': os.environ.get('FREEDOMSHOP_TEST_DB_USER', 'postgres'),
    'HOST': os.environ.get('FREEDOMSHOP_TEST_DB_HOST', '/var/run/postgresql'),
    'PORT': os.environ.get('FREEDOMSHOP_TEST_DB_PORT', ''),
    'PASSWORD': os.environ.get('FREEDOMSHOP_TEST_DB_PASSWORD', ''),
    'TEST': {'NAME': 'test_freedomshop_pos_workflows'},
}}
