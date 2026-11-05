# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, strict readers for POS export files. No backup decoding."""
import csv
import hashlib
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .qbpos_columns import LOOKUPS, heading

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100000
MAX_COLUMNS = 150


class ImportError(ValueError):
    pass


def number(value, label, places=2, signed=True, default=None, digits=12):
    text = str(value or '').strip()
    if not text:
        if default is not None:
            return Decimal(default)
        raise ImportError(f'{label}: a number is required.')
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    try:
        result = Decimal(text.replace('$', '').replace(',', '').strip())
        quantum = Decimal(1).scaleb(-places)
        if (not result.is_finite() or abs(result) >= Decimal(10) ** (digits - places)
                or result.quantize(quantum) != result or (not signed and result < 0)):
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError) as exc:
        raise ImportError(f'{label}: invalid number {value!r} (up to {places} decimal places).') from exc


def boolean(value):
    key = str(value or '').strip().casefold()
    if key in ('', 'no', 'n', 'false', '0'):
        return False
    if key in ('yes', 'y', 'true', '1', 'x'):
        return True
    raise ImportError(f'Tax Exempt: unrecognized value {value!r}.')


def sale_date(value):
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d %H:%M:%S', '%m/%d/%Y %I:%M %p'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ImportError(f'Invalid receipt date {value!r}; use YYYY-MM-DD or MM/DD/YYYY.')


def cell_text(value):
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_file(kind, name, data):
    if kind not in LOOKUPS:
        raise ImportError('Unknown export type.')
    if len(data) > MAX_BYTES:
        raise ImportError('Each export must be 20 MB or smaller.')
    suffix = Path(name).suffix.lower()
    warnings = []
    formula_cells = set()
    if suffix == '.xlsx':
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > MAX_BYTES * 10:
                    raise ImportError('The expanded workbook is too large.')
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            sheet = book.active
            if sheet.max_row > MAX_ROWS + 100 or sheet.max_column > MAX_COLUMNS:
                raise ImportError('The workbook has too many rows or columns.')
            rows = []
            for row_number, row in enumerate(sheet.iter_rows(), 1):
                formula_cells.update((row_number, index) for index, cell in enumerate(row) if cell.data_type == 'f')
                rows.append([cell_text(cell.value) for cell in row])
            if len(book.sheetnames) > 1:
                warnings.append(f'{kind}: only the active worksheet ({sheet.title}) is read.')
            book.close()
        except (BadZipFile, OSError, KeyError, ValueError, TypeError, IndexError, ParseError, InvalidFileException) as exc:
            raise ImportError(f'{kind}: cannot read XLSX: {exc}') from exc
        encoding = 'xlsx'
    elif suffix in ('.csv', '.txt', '.tsv'):
        try:
            decoded = data.decode('utf-8-sig')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            try:
                decoded = data.decode('cp1252')
            except UnicodeDecodeError as exc:
                raise ImportError('Use UTF-8 or Windows cp1252 text.') from exc
            encoding = 'cp1252'
        if '\x00' in decoded:
            raise ImportError('Binary files and UTF-16 text are not supported. Save as UTF-8 CSV.')
        try:
            dialect = csv.Sniffer().sniff(decoded[:65536], delimiters=',\t')
        except csv.Error:
            dialect = csv.excel_tab if '\t' in decoded else csv.excel
        try:
            rows = []
            for row in csv.reader(io.StringIO(decoded, newline=''), dialect=dialect, strict=True):
                if len(row) > MAX_COLUMNS or len(rows) > MAX_ROWS + 50:
                    raise ImportError('Too many rows or columns.')
                rows.append([cell_text(value) for value in row])
        except csv.Error as exc:
            raise ImportError(f'{kind}: malformed delimited file: {exc}') from exc
    else:
        raise ImportError('Use CSV, tab-delimited TXT/TSV, or XLSX. Convert old XLS in Excel; .qpb backups are not accepted.')
    # Reports may have a title before the column row. Do not guess missing fields.
    header_index = None
    required = {'items': {'name'}, 'customers': set(), 'vendors': {'company'},
                'sales': {'receipt', 'date', 'quantity', 'item', 'price', 'extension', 'tax'}}[kind]
    for index, row in enumerate(rows[:50]):
        fields = {LOOKUPS[kind].get(heading(value)) for value in row if value}
        if required.issubset(fields) and (kind != 'customers' or fields & {'last_name', 'full_name', 'company'}):
            header_index = index
            break
    if header_index is None:
        raise ImportError(f'{kind}: no supported column headings found. Required: {", ".join(sorted(required)) or "customer name"}.')
    headers = rows[header_index]
    if len(headers) > MAX_COLUMNS or len(rows) - header_index - 1 > MAX_ROWS:
        raise ImportError('Too many rows or columns.')
    columns = [LOOKUPS[kind].get(heading(value)) for value in headers]
    mapped = [value for value in columns if value]
    if len(mapped) != len(set(mapped)):
        raise ImportError(f'{kind}: two columns map to the same field. Keep one of each alias.')
    unknown = [value for value, key in zip(headers, columns) if value and not key]
    if unknown:
        warnings.append(f'{kind}: unknown columns ignored: {", ".join(unknown)}')
    if header_index:
        warnings.append(f'{kind}: skipped {header_index} title rows before the headings.')
    records = []
    for row_number, row in enumerate(rows[header_index + 1:], header_index + 2):
        if not any(row):
            continue
        if len(row) > len(headers) and any(row[len(headers):]):
            raise ImportError(f'{kind} row {row_number}: more values than column headings.')
        if any(key and (row_number, index) in formula_cells for index, key in enumerate(columns)):
            raise ImportError('Formulas in imported columns are not accepted. Save a copy with values only.')
        record = {key: row[index] if index < len(row) else '' for index, key in enumerate(columns) if key}
        record['_row'] = row_number
        records.append(record)
    return {'kind': kind, 'name': Path(name).name, 'sha256': hashlib.sha256(data).hexdigest(),
            'encoding': encoding, 'unknown_columns': unknown, 'warnings': warnings, 'rows': records}


def read_path(kind, path):
    with Path(path).open('rb') as stream:
        return parse_file(kind, Path(path).name, stream.read(MAX_BYTES + 1))
