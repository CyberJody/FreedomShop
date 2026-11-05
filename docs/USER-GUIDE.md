# FreedomShop User Guide

FreedomShop · Point of Sale is a pre-release. This guide follows the implemented screens. For server setup, see [INSTALL.md](../INSTALL.md).

## Sign in and cashiers

Choose a login and enter its password on Sign In. An admin login enables Admin Mode, including reports and business and tax settings. For shared registers, sign in with the register login, then use **Cashier** to choose the person ringing the transaction and enter their four-digit PIN. A cashier PIN is separate from a login password.

The active cashier appears on the transaction screen. Switch cashiers when another operator takes over, and use Sign Out when leaving the register. The Navigator links to sales, purchasing, inventory and available admin tools; Quick Find searches the selected record type.

## Make a Sale

Open **Make a Sale**. Select a customer when needed, then scan or search for items. Check quantities, amounts and tax status. The editor supports line and receipt discounts, with reasons where required, and miscellaneous items. Choose payment methods and amounts; **Add Another Payment Method** records a split payment. These entries record payment information; they do not charge a card terminal.

Use **Save Only** or **Save & Print** to finish. **Put on Hold** saves an unfinished receipt; choose its hold duration and reopen it from Held Receipts. Stock holds require a real customer and expire at the selected time. A completed sale marked Hold for Pickup appears in Awaiting Pickup, which is separate from an unfinished held receipt.

## Returns

Choose **Point of Sale → Accept Return**, find the original receipt, and select the lines and quantities being returned. The dialog shows the quantity still available to return. Enter the return reason and any note; mark damaged pieces that must not return to stock. Add the selected items and record the refund method before saving the return receipt.

Review any restocking-fee option and the final totals before completing. Completed receipts have correction controls subject to date and admin restrictions; when a receipt can no longer be changed, use the return workflow. This preserves the original sale and its return relationship.

## Sales orders, deposits and layaways

Open **Sales Order**, select the customer and add the ordered items. Save the order before recording payments or pickups. The Payments and Pickup panel shows payments, balance due, applied payments and unused payments. Enter the amount, method and reference, then choose **Record Payment**. Negative payments record refunds. Payments are dated on the day they are recorded; older payments are corrected with a new payment or refund today.

Layaways use this sales-order and deposit workflow. When goods leave, enter the quantities under **Picking up now** and choose **Complete Selected Pickup**. Record enough payment on the order first. Partial pickups leave the remaining quantity open. Refund unused overpayments before completing or cancelling the order. Imported orders show their original deposit information and restrict changes to the imported portion.

## Customers and accounts

Open Customer List to search, add or edit customer contact details, notes and tax status. The selected customer's detail panel shows account balances and available credit. Enable Charge Account and set an account limit in Edit Customer when the shop permits account purchases.

On a sale, use Charge Account to record an account charge. To reduce the balance, use **Take Payment on Account** from the customer details or **Take Payment for Charge Account** in the sale workflow, then record the actual tender received. Review Customer Balances for owed amounts and credits. Imported opening balances and activity entered in FreedomShop both affect the displayed balance.

## Items and departments

Open Item List to search and inspect items, on-hand quantities, costs, vendor and department. Item details separate on-hand, customer orders, customer holds and available quantity. Reorder reminders link to items at or below their reorder point. Add or edit an item to maintain its description, identifiers and stock-related settings; deleting an item sends it to Items Trash where supported.

Departments organize inventory and select a tax category. New rates follow the category and tax settings when left blank; an explicit item rate is retained. Maintain vendor records in Vendor List so purchase orders and receiving use the correct supplier. Check an item's Print Tags option before creating its tags.

## Purchase orders and receiving

Create a Purchase Order, choose its vendor, and add items, quantities and costs. Review dates and notes before saving. Open PO List to return to an order. A sales order can also generate a PO for special-order items; review item mapping and quantities before saving.

Use **Receive Items** to create a receiving voucher, including arrivals against a PO. Check the quantities and costs, then receive the voucher to update stock. Held Vouchers stores unfinished work; Receiving History shows posted vouchers. To correct a posted receiving mistake, use **Reverse Voucher**, then receive the corrected quantities again. Imported and linked documents restrict edits to preserve their history.

## Truck receiving by serial

Print the PO's piece labels first. Open **Truck Receiving** and scan a piece serial or type its code and press Enter. The first valid scan starts a truck session. Check the status panel after each scan; it distinguishes accepted pieces, repeats and errors. The truck table groups pieces by vendor and PO and shows ordered, already received and scanned quantities.

Remove an incorrect scan before choosing **Receive Items**. Receiving creates one voucher per PO; unscanned pieces remain due. Cancel Truck discards that truck's unreceived scans. A browser scanner must send its decoded text to the focused scan box; the workflow uses the piece code to locate the PO line.

## Labels and tags

Use Tag Generator for an item tag or a saved furniture set tag. Item tags use the Item List text, show a preview and allow text changes before sending a 3 × 4 inch tag to a Zebra printer. Print Tags must be enabled for the item. An item's tag page also offers a Letter tag; its text comes from the item, independently of Zebra preview edits.

Furniture set tags collect several items into a Letter sign and can be saved, reopened or downloaded as PDF. PO piece labels identify individual pieces for truck receiving. Choose the intended printer and copy count before printing. Business Settings supplies the shop name, logo and label settings; printer connections are configured separately.

## Reports

Reports require Admin Mode. Choose a report, select its period or filters, then **Display Report**. The six standard reports support Excel and PDF downloads. Daily Totals also offers Direct Print. Period choices include custom dates; tax-year periods use the configured start month.

**Daily Totals** shows money received, tender buckets, new sales, tax and document detail by day. **Sales Tax** presents cash-basis amounts by tax location, with summary or receipt detail; estimates and adjustments are identified in the report. Tax Settings stores a basis choice, but accrual reporting is not implemented. If the tax year is unset after importing an existing database, choose it in Tax Settings before running reports.

**Inventory On Hand** shows current quantities and inventory cost by department. **Customer Balances** lists amounts owed or credits. **General Sales** lists sold items with optional columns and grouping. **Sales by Vendor** offers item summaries, vendor totals and receipt-line detail. These reports measure different activity, so compare their column definitions before comparing totals.

## Settings: business, tax, printers and users

**File → Business Settings** controls shop details, contact information, logo, return-policy text, sales-order wording, copy defaults and serial-prefix settings. Its test receipt is a preview and sends nothing to a printer. **Run setup again** changes setup-only fields such as time zone, currency and Live shop. Restart after a time-zone change. Currency is stored; printed amounts still use the dollar symbol.

**File → Tax Settings** controls whether new entries default to tax, the default location and rate, tax-year start, filing frequency, locations and categories. Turning tax off changes new defaults; existing item rates remain. Used locations and categories can be retired but cannot be renamed or deleted. Recorded historical payment tax is preserved when current rates change. **File → Printers** manages receipt, Letter and Zebra connections; test printing sends a real job.

Setup creates the first admin, optional register login and PIN cashiers. A dedicated Users & PINs management screen is not built yet. For later user maintenance, an administrator uses the transactional `load_app_users` command described in [accounts/README.md](../accounts/README.md); omitted names are deactivated. Rerunning setup skips login creation when an admin already exists.

## Migrate from QuickBooks POS

FreedomShop imports QuickBooks POS exports in CSV or XLSX form: an admin opens **File → Migrate from QuickBooks POS**, uploads the exports, reviews the dry run and then imports. [MIGRATE-FROM-QUICKBOOKS-POS.md](MIGRATE-FROM-QUICKBOOKS-POS.md) lists the expected files and columns. Preserve the original exports and a backup of the source system; `.qpb` backup files are not accepted.

The code currently includes an advanced rebuild command for a prepared database, with history, balance and cutover options. Producing that input requires separate tooling and is not a public backup-decoding workflow. Migration should be rehearsed in a separate installation, with record counts, stock, balances, open orders and report totals checked before cutover. QuickBooks POS is a factual source-system reference, not FreedomShop branding.

## FreedomBooks exchange

Open **Reports → FreedomBooks exchange** as an admin. Choose one business day or a date range of up to 366 days. Inspect the summary, source diagnostics and checks against Daily Totals and Sales Tax. Download one day as JSON or a range as ZIP. The terminal equivalent is `manage.py export_exchange`; see the [exchange guide](EXCHANGE.md) for its options and format.

This stage creates local files and command proposals. Posting requires a compatible FreedomBooks importer, account and tax mappings, and destination review. Resolve failed checks before importing. An unchanged day has stable request keys and a checksum; a changed day can keep its keys, so an importer must stop for reconciliation rather than silently treating a correction as already imported. Preserve the shop's exchange identity when restoring or moving the same shop.

## Notes & Requests and project help

Notes & Requests is the shop's own notebook for requests, status, notes and attachments. Open it from the Navigator or Point of Sale menu. Its records remain in this installation and are not sent to the project.

Use [GitHub Issues](https://github.com/CyberJody/FreedomShop/issues) for public project questions and bugs; [Discussions](https://github.com/CyberJody/FreedomShop/discussions) for open questions and ideas. **File → About FreedomShop** shows the installed version. Include the version, steps and generated sample records. Keep customer details, credentials and private attachments out of public reports. Follow [SECURITY.md](../SECURITY.md) for vulnerabilities.
