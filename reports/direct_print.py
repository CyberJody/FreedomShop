# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct printing of report PDFs on a shop's Letter (Brother) printers.

The PDF export is the print master: it is rasterized at the Letter printer's 300 dpi and sent through the same IPP /
PWG raster path that sales orders use, so what prints matches the PDF page for page. Landscape pages are rotated to
feed as portrait Letter sheets.
"""
from django.utils import timezone

from sales.letter_printing import DPI, PAGE_H, PAGE_W, send_letter
from sales.models import Printer
from sales.printing import PrintError


def letter_printers():
    return Printer.objects.filter(active=True, kind='letter').order_by('name')


def pages_from_pdf(pdf_bytes, color=False):
    """Letter pages at 300 dpi. color=True (a color printer) keeps pages that hold color as RGB; pages
    without any color are still returned as 8-bit gray, so they print monochrome."""
    import pypdfium2 as pdfium
    from PIL import Image
    from sales.letter_printing import has_color

    document = pdfium.PdfDocument(pdf_bytes)
    pages = []
    try:
        for index in range(len(document)):
            image = document[index].render(scale=DPI / 72, grayscale=not color).to_pil()
            image = image.convert('RGB') if color else image.convert('L')
            if color and not has_color(image):
                image = image.convert('L')
            if image.width > image.height:
                image = image.rotate(-90, expand=True)
            if image.size != (PAGE_W, PAGE_H):
                sheet = Image.new(image.mode, (PAGE_W, PAGE_H), 'white')
                image.thumbnail((PAGE_W, PAGE_H))
                sheet.paste(image, ((PAGE_W - image.width) // 2, (PAGE_H - image.height) // 2))
                image = sheet
            pages.append(image)
    finally:
        document.close()
    if not pages:
        raise PrintError('The report has no pages to print.')
    return pages


def print_report(printer, pdf_bytes, job_name):
    """Send a rendered report PDF to a Letter printer. Returns a status message."""
    if printer is None or not printer.active or printer.kind != 'letter':
        raise PrintError('Choose a Letter printer.')
    pages = pages_from_pdf(pdf_bytes, color=bool(printer.color))
    send_letter(printer, pages, job_name)
    stamp = timezone.localtime().strftime('%-I:%M %p')
    return f'Sent {len(pages)} page{"s" if len(pages) != 1 else ""} to {printer.name} at {stamp}.'
