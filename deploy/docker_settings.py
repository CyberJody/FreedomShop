# SPDX-License-Identifier: AGPL-3.0-or-later
"""Container settings. Native installs use settings_site.py instead."""
from config.settings import *  # noqa: F403

SECRET_KEY = os.environ["FREEDOMSHOP_SECRET_KEY"]
if not SECRET_KEY or SECRET_KEY == "unset":
    raise RuntimeError("Set FREEDOMSHOP_SECRET_KEY")
DATABASES["default"].update({  # noqa: F405
    "HOST": os.environ.get("FREEDOMSHOP_DB_HOST", "db"),
    "PORT": os.environ.get("FREEDOMSHOP_DB_PORT", "5432"),
    "PASSWORD": os.environ["FREEDOMSHOP_DB_PASSWORD"],
})
SECURE_SSL_REDIRECT = os.environ.get("FREEDOMSHOP_HTTPS", "0") == "1"
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT
PRINT_TESTING_MARKS = True
PIECE_LABEL_URL = ""
MEDIA_URL = "/media/"
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.staticfiles"]  # noqa: F405
STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "static"  # noqa: F405
