# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated local test settings: never touches the deployment database."""
SECRET_KEY = 'only-for-isolated-tests'
DEBUG = False
ALLOWED_HOSTS = ['testserver', 'localhost']
ROOT_URLCONF = 'config.urls'
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.sessions', 'accounts', 'django.contrib.contenttypes', 'customers', 'sales', 'purchasing', 'reports', 'tags', 'tracker', 'shop_settings']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': ['templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'accounts.admin_mode.context', 'accounts.cashiers.context', 'accounts.server_status.health', 'shop_settings.context.context'], 'builtins': ['sales.templatetags.quantity_tags']}}]
MIDDLEWARE = []
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True

TIME_ZONE = "America/New_York"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
POS_REQUIRE_LOGIN = False

# attachment files go to a throwaway temp folder, never into the repo.
import tempfile  # noqa: E402
TRACKER_UPLOAD_DIR = tempfile.mkdtemp(prefix='fs-tracker-test-')
TRACKER_MAX_UPLOAD = 250 * 1024 * 1024

# Server Status: no hardware status file in tests unless a test points this at a fixture (override_settings).
HW_STATUS_FILE = '/nonexistent/freedomshop/hw.json'

# Server Status certificate row: a path that never exists here (tests mock the reads).
SERVER_CERT_PATHS = ['/etc/ssl/freedomshop-test/cert.pem']

# Printed logo: the shop's artwork is not in the repo, so tests draw a neutral placeholder of the same shape
# (1216 x 650, black on white) that the layouts scale and place exactly like a real logo.
from config.test_logo import placeholder_logo  # noqa: E402
PRINT_LOGO_FILE = placeholder_logo()

# Shop settings seed: the existing tests were written for one shop's setup (Springfield 7 % / Exempt / March tax year),
# so the test database starts with that configuration instead of a new shop's defaults (shop_settings/seeds/).
from pathlib import Path  # noqa: E402
SHOP_SETTINGS_SEED = Path(__file__).resolve().parent.parent / 'shop_settings' / 'seeds' / 'test_shop.json'

# Business Settings logo uploads go to a throwaway temp folder.
MEDIA_ROOT = tempfile.mkdtemp(prefix='fs-test-media-')
