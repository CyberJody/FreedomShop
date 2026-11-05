# SPDX-License-Identifier: AGPL-3.0-or-later
"""About FreedomShop: product, version, license and where to get help. Open without signing in."""
from pathlib import Path

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from .__version__ import __version__

LICENSE_FILE = Path(__file__).resolve().parent.parent / 'LICENSE'
LICENSE_URL = 'https://github.com/CyberJody/FreedomShop/blob/main/LICENSE'


@require_safe
def about(request):
    return render(request, 'about/about.html', {'version': __version__, 'license_url': LICENSE_URL})


@require_safe
def license_text(request):
    try:
        text = LICENSE_FILE.read_text(encoding='utf-8')
    except OSError:
        text = f'The license text is missing from this installation. Read it at {LICENSE_URL}\n'
    return HttpResponse(text, content_type='text/plain; charset=utf-8')
