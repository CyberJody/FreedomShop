# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exit nonzero unless the application and its database answer /health/."""
import json
import urllib.request

request = urllib.request.Request("http://127.0.0.1:8001/health/", headers={"X-Forwarded-Proto": "https"})
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 200 or json.load(response).get("database") != "ok":
        raise SystemExit(1)
