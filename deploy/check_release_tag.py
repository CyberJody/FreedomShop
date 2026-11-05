# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refuse to publish an archive under a tag for a different version."""
import os
from pathlib import Path

from release import version_from_text

root = Path(__file__).resolve().parent.parent
version = version_from_text((root / "freedomshop/__version__.py").read_text())
if os.environ.get("GITHUB_REF_NAME") != f"v{version}":
    raise SystemExit("Tag must equal v + freedomshop/__version__.py; update the version before tagging")
