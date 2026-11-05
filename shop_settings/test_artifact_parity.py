# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compare actual print artifacts; the original 25-output checksum oracle remains unchanged.

The reference renderers hard-code the original shop identity, so the current run's Business row is filled, inside the
test transaction only, from those reference modules' own constants and literals (legacy_identity). No identity value
is written in this file or any fixture; the demo identity in seeds/test_shop.json is replaced for this test alone."""
import io
import importlib
import shutil
import subprocess
import sys
import tempfile
import types
import xml.etree.ElementTree as ET
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock, skipUnless
from zipfile import ZipFile

from django.db import transaction
from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from shop_settings import test_print_parity as original

ROOT = Path(__file__).resolve().parent.parent
# The original recording commit, immediately before the Business rewiring. Read only; never copy its source or
# shop identity into new fixtures. This comparison requires the repository's local history (no network access).
REFERENCE = 'b819bc1'
PRINT_MODULES = ('sales.printing', 'sales.letter_printing', 'sales.label_printing', 'sales.print_documents',
                 'purchasing.print_exports', 'purchasing.piece_labels', 'reports.exports', 'reports.tabular_exports',
                 'reports.inventory_exports', 'reports.daily_totals_exports', 'reports.sales_tax_exports', 'tags.sets')


def reference_available():
    # A snapshot without the development history (a public checkout, a release archive) cannot run this comparison.
    try:
        return subprocess.run(['git', 'cat-file', '-e', f'{REFERENCE}^{{commit}}'], cwd=ROOT,
                              capture_output=True).returncode == 0
    except OSError:
        return False


def xlsx_parts(data):
    """Every workbook part, including styles, dimensions, visibility, relationships and print settings.

    Only the creation/modification timestamps are incidental; ZIP container timestamps aren't workbook content.
    Keep all other XML and every binary part. Canonical XML makes attribute ordering immaterial.
    """
    parts = {}
    with ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            content = archive.read(name)
            if name.endswith(('.xml', '.rels')):
                root = ET.fromstring(content)
                if name == 'docProps/core.xml':
                    for child in list(root):
                        if child.tag in ('{http://purl.org/dc/terms/}created', '{http://purl.org/dc/terms/}modified'):
                            root.remove(child)
                content = ET.canonicalize(ET.tostring(root, encoding='unicode')).encode()
            parts[name] = content
    return parts


def pdf_pages(data):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(data)
    try:
        pages = []
        for page in document:
            textpage = page.get_textpage()
            bitmap = page.render(scale=1)
            try:
                pixels = bitmap.to_pil()
                pages.append((textpage.get_text_range(), pixels.mode, pixels.size, pixels.tobytes()))
            finally:
                textpage.close()
                bitmap.close()
                page.close()
        return pages
    finally:
        document.close()


@contextmanager
def legacy_renderers():
    # Install modules in dependency order and restore both import paths and parent-package attributes afterwards.
    # Yields the reference modules by name, so their identity can be read off them (legacy_identity).
    for name in PRINT_MODULES:
        importlib.import_module(name)
    modules = {}
    with ExitStack() as stack:
        for name in PRINT_MODULES:
            package, attribute = name.rsplit('.', 1)
            path = name.replace('.', '/') + '.py'
            source = subprocess.check_output(['git', 'show', f'{REFERENCE}:{path}'], cwd=ROOT)
            module = types.ModuleType(name)
            module.__package__ = package
            module.__file__ = str(ROOT / path)
            stack.enter_context(mock.patch.dict(sys.modules, {name: module}))
            stack.enter_context(mock.patch.object(importlib.import_module(package), attribute, module))
            exec(compile(source, module.__file__, 'exec'), module.__dict__)
            modules[name] = module
        yield modules


def literals(function):
    """String literals of a reference function in first-use order (the compiler keeps one entry per distinct value)."""
    return [value for value in function.__code__.co_consts if isinstance(value, str)]


def legacy_identity(modules):
    """Business Settings values that reproduce what the reference renderers print, read off those modules alone.

    Constants map directly (receipt STORE_LINES and FOOTER, sales order STORE_BLOCK, FOOTER and STORE_CODE, the PO
    header's STORE_ADDRESS, STORE_PHONE and STORE_FAX, RECEIPT_COPIES). The trade name, tagline and thank-you line
    were literals inside the renderers; they are taken from the functions' constants and cross-checked between
    renderers. The logo needs no value: both sides use settings.PRINT_LOGO_FILE while Business.logo stays empty.
    Not sourced (nothing in the reference prints them, so they stay as seeded): address line 2, country, email,
    website, label address (piece_codes is current code on both sides), serial prefix, order copies default.
    """
    printing, letter = modules['sales.printing'], modules['sales.letter_printing']
    exports = modules['purchasing.print_exports']
    legal_name = printing.STORE_LINES[0]
    name = legal_name.split(',')[0]
    receipt, order = literals(printing.render_receipt), literals(letter.render_sales_order)
    for module, function in (('sales.printing', 'render_test_page'), ('sales.letter_printing', 'render_sales_order'),
                             ('sales.label_printing', 'render_tag'), ('reports.exports', 'pdf_bytes')):
        assert name in literals(getattr(modules[module], function)), f'{module}.{function} prints another name'
    def after_name(texts):  # the literal printed under the name, skipping the alignment words both renderers pass
        return next(text for text in texts[texts.index(name) + 1:] if text not in ('left', 'center', 'right'))
    tagline = after_name(receipt)
    assert tagline == after_name(order), 'receipt and sales order print different taglines'
    thanks = [text for text in order if text.startswith('Thank you')]
    assert len(thanks) == 1, thanks
    street, city_line = exports.STORE_ADDRESS
    city, region_postal = city_line.rsplit(', ', 1)
    region, postal_code = region_postal.split(' ', 1)
    assert exports.STORE_NAME == legal_name
    return dict(name=name, legal_name=legal_name, short_code=letter.STORE_CODE, address_line1=street, city=city,
                region=region, postal_code=postal_code, phone=exports.STORE_PHONE, fax=exports.STORE_FAX,
                tagline=tagline, receipt_header_lines='\n'.join(printing.STORE_LINES), receipt_footer=thanks[0],
                return_policy=printing.FOOTER, order_return_policy=letter.FOOTER,
                order_address_lines='\n'.join(letter.STORE_BLOCK), receipt_copies=printing.RECEIPT_COPIES, logo='')


def use_identity(values):
    """Save the values on this test's Business row; the test transaction rolls them back with everything else."""
    from shop_settings.config import business, clear_cache
    row = business()
    for field, value in values.items():
        setattr(row, field, value)
    row.full_clean()
    row.save()
    clear_cache()


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return original.NOW.astimezone(tz) if tz else original.NOW.replace(tzinfo=None)


@contextmanager
def deterministic_metadata():
    # Freeze the writers, not document content: PDF IDs/dates, XLSX core dates and ZIP-entry timestamps.
    frozen = types.SimpleNamespace(datetime=FrozenDatetime, timezone=timezone)
    with ExitStack() as stack:
        stack.enter_context(mock.patch('django.utils.timezone.now', return_value=original.NOW))
        stack.enter_context(mock.patch('reportlab.rl_config.invariant', 1))
        stack.enter_context(mock.patch('PIL.PdfImagePlugin.time.gmtime', return_value=original.NOW.utctimetuple()))
        stack.enter_context(mock.patch('zipfile.time.localtime', return_value=original.NOW.timetuple()))
        stack.enter_context(mock.patch('openpyxl.packaging.core.datetime', frozen))
        stack.enter_context(mock.patch('openpyxl.writer.excel.datetime', frozen))
        yield


class Artifact(str):
    """Carry actual output through the original fixture's checksum calls without changing that test's assertions."""
    def __new__(cls, digest, kind, data):
        value = super().__new__(cls, digest)
        value.kind, value.data = kind, data
        return value


def capture_outputs():
    import hashlib
    from sales import letter_printing
    raster_jobs = {}

    def raster(image):
        return image.mode, image.size, image.tobytes()

    def capture_pages(name, render):
        def wrapped(*args, **kwargs):
            pages = render(*args, **kwargs)
            raster_jobs.setdefault(name, tuple(raster(page) for page in pages))
            return pages
        return wrapped

    with transaction.atomic(), ExitStack() as stack:
        # Each run starts with the same rows and counters; rollback restores both before the other renderer runs.
        stack.enter_context(mock.patch.object(original, 'image_hash', lambda image: Artifact(original_image_hash(image), 'raster', (raster(image),))))
        for function, kind in (('pdf_hash', 'pdf'), ('xlsx_hash', 'xlsx')):
            stack.enter_context(mock.patch.object(original, function, lambda data, kind=kind: Artifact(hashlib.sha256(data).hexdigest(), kind, data)))
        for name, function in (('letter_test_page', 'render_letter_test_page'), ('sales_order', 'render_sales_order')):
            stack.enter_context(mock.patch.object(letter_printing, function, capture_pages(name, getattr(letter_printing, function))))
        output = original.PrintParityTests().outputs()
        result = {name: (value.kind, value.data) if isinstance(value, Artifact)
                  else ('raster', raster_jobs[name]) if name in raster_jobs else ('value', value)
                  for name, value in output.items()}
        transaction.set_rollback(True)
    return result


original_image_hash = original.image_hash


def render_spreadsheets(outputs, directory):
    """Render real spreadsheet pages with an isolated, headless LibreOffice profile."""
    inputs, rendered = directory / 'input', directory / 'pdf'
    inputs.mkdir()
    rendered.mkdir()
    for name, (kind, data) in outputs.items():
        if kind == 'xlsx':
            (inputs / f'{name}.xlsx').write_bytes(data)
    command = [shutil.which('libreoffice'), f'-env:UserInstallation={(directory / "profile").as_uri()}',
               '--headless', '--convert-to', 'pdf', '--outdir', str(rendered), *map(str, sorted(inputs.glob('*.xlsx')))]
    process = subprocess.run(command, capture_output=True, timeout=60)
    if process.returncode:
        raise AssertionError(process.stderr.decode(errors='replace'))
    return {path.stem: path.read_bytes() for path in rendered.glob('*.pdf')}


class SpreadsheetOracleTests(SimpleTestCase):
    def assert_workbooks_match(self, expected, actual):
        self.assertEqual(xlsx_parts(expected), xlsx_parts(actual))

    def test_oracle_rejects_visible_changes_even_when_values_match(self):
        book = Workbook()
        book.active['A1'] = 0.25
        stream = io.BytesIO()
        book.save(stream)
        original = stream.getvalue()
        for change in ('format', 'font', 'hidden', 'width', 'print'):
            with self.subTest(change=change):
                changed = load_workbook(io.BytesIO(original))
                sheet = changed.active
                if change == 'format':
                    sheet['A1'].number_format = '0%'
                elif change == 'font':
                    sheet['A1'].font = Font(color='FFFFFF')
                elif change == 'hidden':
                    sheet.column_dimensions['A'].hidden = True
                elif change == 'width':
                    sheet.column_dimensions['A'].width = 40
                else:
                    sheet.page_setup.orientation = 'landscape'
                out = io.BytesIO()
                changed.save(out)
                with self.assertRaises(AssertionError):
                    self.assert_workbooks_match(original, out.getvalue())


@skipUnless(reference_available(), f'needs the development history containing commit {REFERENCE}')
class PrintArtifactParityTests(TestCase):
    def test_pre_business_and_current_print_artifacts_match(self):
        self.assertIsNotNone(shutil.which('libreoffice'), 'LibreOffice is required for spreadsheet print parity.')
        with deterministic_metadata():
            with legacy_renderers() as legacy:
                use_identity(legacy_identity(legacy))
                expected = capture_outputs()
            actual = capture_outputs()
        self.assertEqual(set(expected), set(actual))
        self.assertEqual(len(expected), 25)
        for name, (kind, data) in expected.items():
            with self.subTest(output=name):
                actual_kind, actual_data = actual[name]
                self.assertEqual(kind, actual_kind)
                # All exports are deterministic under the frozen writer metadata. This establishes byte identity
                # independently of the original checksum oracle, including the complete spreadsheet ZIP bytes.
                self.assertTrue(data == actual_data, f'{name}: output bytes/pixels differ')
                if kind == 'pdf':
                    before, after = pdf_pages(data), pdf_pages(actual_data)
                    self.assertEqual(len(before), len(after), f'{name}: page count')
                    self.assertTrue(before == after, f'{name}: PDF text or page pixels differ')
                elif kind == 'xlsx':
                    self.assertEqual(xlsx_parts(data), xlsx_parts(actual_data), name)
        with tempfile.TemporaryDirectory(prefix='fs-print-parity-') as folder:
            root = Path(folder)
            (root / 'before').mkdir()
            (root / 'after').mkdir()
            before = render_spreadsheets(expected, root / 'before')
            after = render_spreadsheets(actual, root / 'after')
            xlsx_names = {name for name, (kind, _) in expected.items() if kind == 'xlsx'}
            self.assertEqual(set(before), xlsx_names)
            self.assertEqual(set(after), xlsx_names)
            for name in sorted(xlsx_names):
                with self.subTest(spreadsheet=name):
                    first, second = pdf_pages(before[name]), pdf_pages(after[name])
                    self.assertEqual(len(first), len(second), f'{name}: printed page count')
                    self.assertTrue(first == second, f'{name}: printed text or page pixels differ')
