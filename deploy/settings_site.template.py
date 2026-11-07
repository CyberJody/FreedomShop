# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local settings, rendered by install.sh. Keep this file outside releases."""
from config.settings import *  # noqa: F403

SECRET_KEY_FILE = Path("@@SECRET_KEY_FILE@@")
SECRET_KEY = SECRET_KEY_FILE.read_text().strip()
if not SECRET_KEY or SECRET_KEY == "unset":
    raise RuntimeError("The FreedomShop secret key is missing")
ALLOWED_HOSTS = "@@ALLOWED_HOSTS@@"
DATA_DIR = Path("@@DATA_DIR@@")
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"
TRACKER_UPLOAD_DIR = DATA_DIR / "uploads" / "fixes"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_SETTINGS_FILES = [Path(__file__).resolve(), SECRET_KEY_FILE]
BACKUP_SCHEDULE_ENABLED_DEFAULT = True  # freedomshop-backup.timer runs the Backups page schedule.
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "freedomshop",
    "USER": "freedomshop",
    "HOST": "/var/run/postgresql",
    "CONN_MAX_AGE": 60,
}}
# HTTP on the LAN by default. Enable all three when adding HTTPS manually.
SECURE_SSL_REDIRECT = "@@HTTPS@@"
SESSION_COOKIE_SECURE = "@@HTTPS@@"
CSRF_COOKIE_SECURE = "@@HTTPS@@"
SECURE_SSL_HOST = None
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
PRINT_TESTING_MARKS = True  # The setup wizard's Live shop switch controls the marks.
PIECE_LABEL_URL = ""  # Bare serials until configured in Business Settings.
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.staticfiles"]  # noqa: F405
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # noqa: F405
