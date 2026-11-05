# SPDX-License-Identifier: AGPL-3.0-or-later
"""A neutral placeholder logo for tests: no shop's artwork lives in the repo."""
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = (1216, 650)


def placeholder_logo():
    """Write a black-on-white placeholder (a ring and a bar, edge to edge like trimmed artwork) and return its path."""
    path = Path(tempfile.mkdtemp(prefix='fs-test-logo-')) / 'logo.png'
    image = Image.new('L', SIZE, 255)
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, 649, 649), outline=0, width=60)
    draw.rectangle((700, 200, 1215, 450), fill=0)
    draw.rectangle((0, 0, 1215, 30), fill=0)
    image.save(path)
    return str(path)
