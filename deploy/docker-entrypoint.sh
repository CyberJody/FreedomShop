#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
if [[ "${1:-}" == manage.py ]]; then
    shift
    exec python /app/manage.py "$@"
fi
if [[ "${1:-}" == gunicorn ]]; then
    python /app/manage.py migrate --noinput
    python /app/manage.py collectstatic --noinput
    python /app/manage.py check
fi
exec "$@"
