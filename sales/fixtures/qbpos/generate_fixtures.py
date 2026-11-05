# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reproduce made-up encoding/XLSX fixtures; never reads shop exports."""
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent
ROOT.joinpath('items-cp1252.txt').write_bytes(
    'Description 1\tDescription 2\tDepartment\tAttr\tSize\tPrice 1\tAvg. Cost\tOn Hand\tTax Code\n'
    'Café Table\tDemo café table\tDining\tNatural\tSmall\t75.00\t30.25\t2\tTax\n'.encode('cp1252'))
book = Workbook()
sheet = book.active
sheet.title = 'Demo Items'
sheet.append(['Demo export — made-up data'])
sheet.append(['Item Number', 'Description 1', 'Description 2', 'Department', 'Price 1', 'Average Cost', 'Qty On Hand', 'Tax Code', 'UPC'])
sheet.append([201, 'Demo Stool', 'Demo upholstered stool', 'Dining', 45, 12.50, 4, 'Tax', '001234567899'])
book.save(ROOT / 'items.xlsx')
