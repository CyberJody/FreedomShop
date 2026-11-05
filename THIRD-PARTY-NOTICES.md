# Third-party notices

FreedomShop is licensed under AGPL-3.0-or-later. Its dependencies retain their own licenses. This inventory covers every direct dependency in `requirements.in`, checked against installed distribution metadata and license files on 2026-11-04. Versions below match `requirements.lock`; dependency updates require a fresh check.

| Dependency | Checked version | License | Installed evidence |
| --- | --- | --- | --- |
| Django | 5.2.17 | BSD-3-Clause | `License-Expression`; `licenses/LICENSE` (also ships `LICENSE.python`) |
| gunicorn | 23.0.0 | MIT | `License` and MIT classifier; `LICENSE` |
| psycopg | 3.3.6 | LGPL-3.0-only | `License-Expression`; `licenses/LICENSE.txt` |
| psycopg-binary, selected by `psycopg[binary]` | 3.3.6 | LGPL-3.0-only | `License-Expression`; `licenses/LICENSE.txt` |
| Pillow | 12.3.0 | MIT-CMU | `License-Expression`; `licenses/LICENSE` |
| openpyxl | 3.1.5 | MIT | `License` and MIT classifier; `LICENCE.rst` |
| reportlab | 4.5.1 | BSD-3-Clause | BSD metadata; all three conditions in `licenses/LICENSE` |
| pypdfium2 | 5.13.0 | Apache-2.0 OR BSD-3-Clause for the wrapper; bundled dependencies have additional licenses | `License`; `licenses/LICENSES/Apache-2.0.txt`, `BSD-3-Clause.txt` and bundled build notices |
| segno | 1.6.6 | BSD-3-Clause | BSD classifier; all three conditions in `licenses/LICENSE` |

Evidence paths are relative to each package's `.dist-info` directory in the installed environment. Metadata may use the shorter label “BSD”; the installed license text establishes the three-clause form for reportlab and segno.

## Bundled components

pypdfium2 wheels include PDFium and other components. Retain the complete wheel license directories, including `licenses/data/linux_x64/BUILD_LICENSES/` for the checked Linux wheel. The metadata lists PDFium's BSD notice, additional component notices, and `CC-BY-4.0.txt` alongside the wrapper licenses. Do not describe the whole binary bundle as having just one license. Other platforms may ship different build-license paths.

The binary psycopg distribution also contains native dependencies. Preserve its complete distribution and associated notices. Pillow and Django ship additional component notices with their installed packages; retain those too. This table is an inventory of direct requirements, not a replacement for full wheel or system-library notices.

## Fonts shipped in the repository

The DejaVu fonts under `sales/print_assets/fonts/` use the Bitstream Vera license, with DejaVu changes in the public domain. Their notice is preserved in [LICENSE-DejaVu.txt](sales/print_assets/fonts/LICENSE-DejaVu.txt). Keep it with redistributed fonts.

## Redistribution

Keep the copyright, license and attribution files supplied with dependencies. Source releases refer to dependencies through requirements files; distributions that bundle wheels, fonts or system libraries must retain those components' notices. The application's SPDX headers identify FreedomShop source and do not replace third-party license terms.
