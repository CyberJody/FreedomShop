# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the original demo PNG: text on a colour block, no external artwork."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[2]
image = Image.new('RGB', (720, 180), '#294B57')
font = ImageFont.truetype(str(root / 'sales/print_assets/fonts/DejaVuSans.ttf'), 44)
ImageDraw.Draw(image).text((30, 58), 'Demo Home & Hardware', fill='white', font=font)
image.save(Path(__file__).with_name('demo-logo.png'))
