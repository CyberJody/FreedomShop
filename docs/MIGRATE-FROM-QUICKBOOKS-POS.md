# Migrate from QuickBooks POS

Sign in as an **Admin**, open **File → Business Settings**, then select
**Migrate from QuickBooks POS**. The same entry is also in the File menu.
Upload exports, review the dry run, save any department/tax mapping changes,
then select **Import**. Back up the FreedomShop database before a migration.

This feature reads files produced from QuickBooks POS list/report exports.
It does not read `.qpb` backups. The existing `rebuild_from_backup` command
and `sales/rebuild.py` remain available for shops that already have a decoded
SQLite dataset; producing that dataset is outside this product workflow.

## Export the lists

Intuit documents these steps in its [Point of Sale 7.0 User's Guide,
chapter 8, pp. 127–129](https://http-download.intuit.com/http.intuit/OpenCms/sites/default/QBSupportSite/PDFs/POS70UG.pdf).
The [2013 User Guide, “Export Point of Sale List Data,” pp. 50–51](https://blackrockbusiness.com/wp-content/uploads/2015/12/POS_2013_User_Guide.pdf)
also documents worksheet/start-row selection and configurable templates.

1. In QuickBooks POS, display the inventory item list. Clear filters if you
   want the complete list.
2. Select **I Want To → Export to Excel**, choose the records to export,
   and select **Next**.
3. Choose the destination workbook, worksheet and starting row. Choose the
   export template; **Manage Templates** lets you inspect/change its columns.
4. Complete the wizard. Repeat for **Customers** and **Vendors**.
5. In Excel, save a copy as **XLSX**, **CSV**, or tab-delimited **TXT**.
   Older POS versions export `.xls`; convert it rather than renaming its
   extension. Keep identifiers/UPCs as text to preserve leading zeros.

Excel must be installed for the documented export workflow. Available fields
vary by edition and template. The importer reads UTF-8 (including BOM) or
Windows cp1252 text, comma/tab delimiters, and the active XLSX worksheet.
It accepts up to 20 MB, 100,000 data rows and 150 columns per file. Formula
cells must be replaced with values. A title above the headings is allowed
within the first 50 rows; remove report subtotals and grand totals.

## Columns and mappings

The headings below are supported examples, not a promise that every template
contains them. [The 2013 guide](https://blackrockbusiness.com/wp-content/uploads/2015/12/POS_2013_User_Guide.pdf)
documents configurable labels, Inventory fields, Customer Fields (pp. 304–309),
Vendor Fields (pp. 330–332), and Sales Tax Codes (pp. 243–245).
It identifies **Description 1/Description 2** as older labels for **Item Name/
Item Description**. The complete importer alias list is
[`sales/qbpos_columns.py`](../sales/qbpos_columns.py).

| File | Supported information |
| --- | --- |
| Items | Item #, Item Name, Item Description, Dept Name/Code, Vendor Code, Attribute, Size, Regular Price, Average Unit Cost, Order Cost, On-Hand Qty, UPC, Alternate Lookup, Tax Code, Item Type, Unit of Measure |
| Customers | Last/First/Full Name, Company, Customer #, Phone/Mobile, Address/Address 2, City, State, Zip, Email, Tax Exempt, Notes |
| Vendors | Vendor Code, Company, Contact, Phone, Fax, Address/Address 2, City, State, Zip, Email, Account #, Terms |

Unknown columns are listed in the preview and ignored. Missing quantity,
price or cost becomes zero; missing stock/cost produces a warning. Invalid
numbers, excess decimal precision, ambiguous alias columns and unsupported
item types stop the batch. Review zero costs before accepting inventory value.
Attribute is stored in the existing item's attribute (`wood`) field.

Departments are proposed from item department names. Reuse an existing
department or create one; a blank name becomes “Imported items.” A new
department initially uses its first item's mapped tax category. Every item
keeps its own category, including mixed-code departments. Vendors referenced
only by code get a placeholder; upload the vendor list together with items
to populate its contact details.

`Tax` proposes an active taxable `TaxCategory`; `Non` proposes an active
non-taxable category. Review these proposals against your tax setup.
Other codes need an explicit choice unless they match an existing category
name. Categories use their configured rate or the shop default; turning
“Charge sales tax” off gives new items zero rates. This does not import POS
tax preferences, price thresholds or multiple-agency rules. Configure them
in Tax Settings first. Customer Tax Exempt uses the existing exempt location.

## Opening inventory

The default is **opening receiving vouchers dated today, one per department**.
Each line has the exported on-hand quantity and average unit cost. Its
receiving movement records that quantity and value; the item's stock starts
at the same snapshot. Historical sales never reduce opening stock again.
Voucher/movement amounts use the app's existing cent rounding. For the demo
fixture, `3 × 50.00 + 2 × 65.50 = 281.00` in both inventory and receiving.

Choose **no opening voucher** to set `on_hand` and average cost directly,
without receiving history. Negative opening quantities require this choice
or correction in the export. Service/non-inventory items do not receive stock.
This process does not create vendor payables or alter an accounting ledger.

## Optional Sales History report

Intuit's [7.0 guide, p. 129](https://http-download.intuit.com/http.intuit/OpenCms/sites/default/QBSupportSite/PDFs/POS70UG.pdf)
documents running a report and selecting **Excel** at its bottom. The
[2013 guide, “Export Reports to Excel,” p. 469](https://blackrockbusiness.com/wp-content/uploads/2015/12/POS_2013_User_Guide.pdf)
documents selecting report columns/filters with **Modify Report**, then
selecting **Excel** on the report's top toolbar. Use a sales-history detail report
and export a date range small enough to avoid truncation in older Excel.

The list export utility is not a complete receipt/tender export. This importer
supports a report only when it supplies **one row per sold item**, with:

`Receipt #, Date, Customer, Qty, Item, Price, Ext Price, Tax, Tender`

Receipt #, Date, Qty, Item, Price, Ext Price and Tax are required. Tax must
be the **line's tax**, not a receipt tax repeated on every line. Price is the
exported effective unit price and Ext Price is its final extension. Exclude
subtotals, total rows and duplicated rows. Dates may be ISO or US month/day/year.
Keep stores separate: duplicate receipt numbers across stores cannot be resolved.
An export with fewer details must be retained outside FreedomShop rather than
guessed into receipts.

The importer groups receipt numbers and preserves exported extensions and
tax totals in the existing read-only `Document.imported` models. It does not
recompute their tax. `Tax Rate` is optional; when absent, effective line rates
are inferred from tax/extension, and zero-tax lines are classified non-taxable.
Historical tax locations use the current default/exempt location. These
limitations are warnings in the preview; they prevent this report from being
a complete historical tax filing record. Historical costs/COGS are unknown.

**Tender labels alone cannot reconstruct payments.** Those receipts import
totals only, with no invented cash or account activity. The preview, import
log and receipt notes say so. Daily Totals, Sales Tax, customer balances and
FreedomBooks exchange cannot reconstruct their payments. If an additional
**Tender Amount** column supplies reconciling cash/check/credit/debit/other
payments, those tenders are imported. A repeated identical receipt-level
payment is counted once; distinct explicit payments must sum to the receipt
total. Account and gift tender flows remain totals only because their balances
and applications are absent. No return links are invented.

## Re-import and undo

Rows have provenance `qbpos:<SHA-256 of original file bytes>:<file row>`;
derived departments, placeholders and vouchers add a suffix. Identical-file
re-imports find those rows and update imported master fields, while preserving
current stock and costs. Opening vouchers are created only for newly created
items. Historical receipts from the same source are reused.

Possible item duplicates use **name + attribute + size**; customers use
**name + phone digits**. Matching ignores case and repeated spaces. Vendor
code/company and item/receipt numbers are checked too. Possible duplicates
are skipped and reported, never silently merged. A changed/reordered file has
a different SHA and therefore needs duplicate review; this is not a general
catalog synchronization tool. Avoid splitting a single receipt across files.

**Past imports** lists previews, committed imports, counts and batch IDs.
**Undo this import** removes rows created by that batch and restores its
updates atomically. It refuses if rows changed, acquired sales/receiving/other
dependencies, or were already undone. The batch's own imported receipts and
opening vouchers can be removed together. A no-op re-import owns no earlier
rows. After selling/receiving or editing imported rows, use normal corrections
or restore a database backup instead. Preview data is stored in the database;
on commit the staged export is cleared, while provenance and undo snapshots
remain. Treat database backups as containing customer data.

## Command line

Run the same dry run first (omit files you do not have):

```console
python manage.py import_qbpos --items items.xlsx --customers customers.csv --vendors vendors.csv --sales sales.txt
```

Repeat with `--commit` to load. Even with `--commit`, the command validates a
dry run before writing. Mapping/stock choices:

```console
python manage.py import_qbpos --items items.csv --tax-map 'Tax=Taxable' --tax-map 'Non=Non-taxable' --department-map 'Dining=12' --no-opening-voucher --commit
python manage.py import_qbpos --undo IMPORT-UUID --commit
```

Category names must be active names from your Tax Settings; department values
are existing active department IDs. Both mapping switches may be repeated.
The command prints the batch UUID for the log and undo. Without `--commit`,
business rows and the import log are not saved.

Not imported: open orders, deposits, balances, credits, gift certificates,
purchase orders, past receiving, pictures, assemblies/groups, serial history,
employees/PINs, preferences, or a complete historical accounting ledger.
