# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

# The shop's time zone from setup (Business.time_zone) replaces TIME_ZONE here, once; changing it needs a restart.
from shop_settings.config import apply_time_zone  # noqa: E402

apply_time_zone()
