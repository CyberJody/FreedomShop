# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY_FILE = Path(os.environ.get("FREEDOMSHOP_SECRET_KEY_FILE", "/etc/freedomshop/secret-key"))
SECRET_KEY = os.environ.get("FREEDOMSHOP_SECRET_KEY") or (SECRET_KEY_FILE.read_text().strip() if SECRET_KEY_FILE.exists() else "unset")
DATA_DIR = Path(os.environ.get("FREEDOMSHOP_DATA_DIR", "/var/lib/freedomshop"))
DEBUG = False
# Extra host names come from the environment (comma-separated), e.g. FREEDOMSHOP_ALLOWED_HOSTS="shop.example.com,192.0.2.10".
ALLOWED_HOSTS = ["localhost", "127.0.0.1"] + [h.strip() for h in os.environ.get("FREEDOMSHOP_ALLOWED_HOSTS", "").split(",") if h.strip()]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "accounts",
    "customers",
    "sales",
    "purchasing",
    "reports",
    "tags",
    "tracker",
    "shop_settings",
]
MIDDLEWARE = [
    "shop_settings.middleware.BackupMaintenanceMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "shop_settings.middleware.SetupGateMiddleware",  # first-run setup (/setup/) until it is finished
    "accounts.middleware.RequireLoginMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request", "accounts.admin_mode.context", "accounts.cashiers.context", "accounts.server_status.health", "shop_settings.context.context"],
                "builtins": ["sales.templatetags.quantity_tags"]},  # `qty` filter available in every template
}]
# Unix socket + PostgreSQL peer authentication: no database password required.
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.environ.get("FREEDOMSHOP_DB_NAME", "freedomshop"),
    "USER": os.environ.get("FREEDOMSHOP_DB_USER", "freedomshop"),
    "HOST": "/var/run/postgresql",
    "CONN_MAX_AGE": 60,
}}
LANGUAGE_CODE = "en-us"
# The shop's own time zone from setup (Business Settings) replaces this when the app starts (config/wsgi.py); a change
# made in setup takes effect after a restart. This value is the fallback until setup has set one.
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
# Nginx terminates TLS on the customer LAN/VPN and overwrites this header.
# Gunicorn must remain bound to loopback so clients cannot spoof it directly.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
# Canonical HTTPS host name; unset (None) unless the site sets FREEDOMSHOP_SSL_HOST.
SECURE_SSL_HOST = os.environ.get("FREEDOMSHOP_SSL_HOST") or None
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# HSTS is off unless the site opts in (seconds, e.g. 3600 to start); no subdomains or preload.
SECURE_HSTS_SECONDS = int(os.environ.get("FREEDOMSHOP_HSTS_SECONDS", "0"))
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
SESSION_COOKIE_AGE = 43200
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
POS_REQUIRE_LOGIN = True
# Cashier inactivity timeout only; does not change the signed-in session. 0 = off.
CASHIER_TIMEOUT_MINUTES = 0
# Printouts carry testing bars until the shop is marked live in setup (Business "Live shop"). Set this to False in the
# site settings to turn them off whatever setup says; True (the default) leaves it to the Live shop setting.
PRINT_TESTING_MARKS = True


# Keep attachment files outside the application release directory.
TRACKER_UPLOAD_DIR = DATA_DIR / "uploads" / "fixes"
TRACKER_MAX_UPLOAD = 250 * 1024 * 1024  # 250 MB; nginx client_max_body_size must match
# Uploads bigger than this stream to a temp file instead of memory (Django default 2.5 MB). DATA_UPLOAD_MAX_MEMORY_SIZE
# (non-file form fields) stays at its default.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2621440

# Optional root-written hardware and backup status file, readable by the application user.
HW_STATUS_FILE = os.environ.get("FREEDOMSHOP_HW_STATUS_FILE", "")

# PO item labels: the QR code on each label holds this address with the piece code in place of {code}, so a phone
# camera opens the shop's website; the app's scanners read the code out of it (purchasing/piece_codes.py). example.com
# is a placeholder: set the shop's own address in the site settings file (or FREEDOMSHOP_PIECE_LABEL_URL).
PIECE_LABEL_URL = os.environ.get("FREEDOMSHOP_PIECE_LABEL_URL", "https://example.com/?wc={code}")
# Server Status: HTTPS certificate files whose expiry it shows (colon-separated in FREEDOMSHOP_CERT_PATHS; none = no row).
SERVER_CERT_PATHS = [p for p in os.environ.get("FREEDOMSHOP_CERT_PATHS", "").split(":") if p]
# Printer row (its name in Printers) that PO item labels start on. Blank = Tray 1 of the color office Brother. Staff can
# pick another Letter printer on the PO page, and labels remember their own last choice.
PIECE_LABEL_PRINTER = ""
# Shop data outside the code folder: uploads such as the Business Settings logo (MEDIA_ROOT/branding/).
MEDIA_ROOT = DATA_DIR / "media"

# Backups go to BACKUP_DIR, default DATA_DIR / "backups" worked out at run time so a site
# settings module that changes DATA_DIR moves them too. See docs/BACKUPS.md.

# Shared by workers; both cache tables are created by migrations.
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
          'admin_reauthentication': {'BACKEND': 'accounts.reauth_cache.ReauthenticationCache',
                                     'LOCATION': 'accounts_adminreauthenticationcache'},
          'register_overrides': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
                                 'LOCATION': 'shop_settings_registeroverridecache', 'TIMEOUT': 900,
                                 'OPTIONS': {'MAX_ENTRIES': 2147483647, 'CULL_FREQUENCY': 3}}}
