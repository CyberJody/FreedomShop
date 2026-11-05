`fonts/`: DejaVu Sans, DejaVu Sans Condensed and Condensed Bold (Bitstream Vera / DejaVu license, `fonts/LICENSE-DejaVu.txt`), used by the Zebra item tag renderer (`sales/label_printing.py`). Vendored so a server without system fonts can render tags.

The shop's logo is not kept here: `settings.PRINT_LOGO_FILE` points at it. If there is no logo, the layouts fall back to a text heading.
