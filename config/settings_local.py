# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local rehearsal settings: same app, local PostgreSQL, no production secret file."""
from .settings import *  # noqa: F401,F403

SECRET_KEY = "rehearsal-only-not-secret"
DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "freedomshop_rehearsal",
    "HOST": "/var/run/postgresql",
    "CONN_MAX_AGE": 0,
}}
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
# The deployed suite runs without the login gate (the release path sets this too).
POS_REQUIRE_LOGIN = False
