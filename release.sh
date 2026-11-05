#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Usage: ./release.sh [OUTPUT_DIR]. Archive committed HEAD; never creates a tag.
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$SOURCE/deploy/release.py" "$@"
