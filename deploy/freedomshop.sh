#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$SOURCE/operations.py" ]]; then
    exec python3 "$SOURCE/operations.py" "$@"
fi
exec /usr/bin/python3 /usr/local/lib/freedomshop/operations.py "$@"
