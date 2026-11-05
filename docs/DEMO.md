# Demo Home & Hardware

`load_demo` generates fictional home and hardware store data. It creates **no logins, passwords, PINs, cashier identities or printers**. Use the first-run wizard or `manage.py setup` to create identities. No print jobs are sent: generating PO labels only creates the serial records that the label preview uses.

## A separate local test copy

From the project directory, with the project's Python environment active:

```bash
export FREEDOMSHOP_DEMO_DB=/tmp/freedomshop-demo-20261104.sqlite3
python manage.py migrate --settings=config.demo_settings --noinput
python manage.py load_demo --settings=config.demo_settings --with-history --days 120 --seed 7
```

`config.demo_settings` uses a local SQLite file and `/tmp/freedomshop-demo-media` for uploads. It never imports deployment settings or connects to the installed shop database. Set `FREEDOMSHOP_DEMO_DB` to another filename for a fresh copy. Run subsequent setup and application commands with the same settings and environment variable. `FREEDOMSHOP_DEMO_MEDIA` can give each copy its own upload directory.

For an already configured, empty PostgreSQL **test copy**, using its own settings module:

```bash
python manage.py load_demo --settings=test_copy_settings --with-history --days 120 --seed 7
```

For a short automated rehearsal, use `--days 10`. The catalog stays the same size; transaction counts scale with the day window. Without `--with-history`, the command loads the large catalog and business settings only.

## Recommended identity names

Choose credentials privately in the wizard or `manage.py setup`. Suggested login names:

- Admin: `demo-admin` (display name Demo Admin).
- Register: `demo-register` (display name Demo Register).

Create these four PIN cashier identities there if you want new transactions to use the same names as the sample history:

- Demo Aspen
- Demo Birch
- Demo Cedar
- Demo Willow

The loader stamps generated transactions with those names even when the identities have not been created yet. Creating the identities later does not change history.

## Wizard

The existing demo step still offers its small fixture: 3 departments, 12 items, 2 vendors, 6 customers, no history. Select **With 4 months of sample history** to invoke `load_demo --with-history` with 120 days and seed 7 instead. Both choices require an empty shop. The history choice preserves the business details and tax setup from the preceding wizard steps; the command has the same behavior with `--preserve-settings`.

An ordinary command load configures Demo Home & Hardware, 123 Main St, Springfield, 555-0100; a text-on-colour PNG logo; Local tax at 7%, Exempt at 0%, a January tax year and quarterly filing. It leaves setup completion to the wizard or `manage.py setup`.

## What to explore

The large catalog has 12 departments, 250 items, 15 vendors and 120 fictional word-pair customer names. Items have costs, three price levels, vendor codes, reorder points and nonnegative stock, including some zero counts. Services are non-inventory and non-taxable. Twelve customers have charge accounts; sample charges and payments establish their balances. Exempt customers carry fictional certificate numbers in Notes and source metadata because the customer model has no dedicated certificate-number field.

At 120 days, history includes approximately 1,500 ordinary receipts, plus linked returns (about 6%), a few voids, split payments, receipt and line discounts, gift-certificate issues and redemptions, and account payments. Sixty sales orders show deposits, completed pickups, partial pickups, cancellations with deposit refunds, and open balances. Held receipts include expired and no-expiry holds. There is no separate layaway model; deposited orders and held receipts exercise the available workflows.

Forty purchase orders include 30 receiving vouchers and open or partially received orders. Five POs have generated serial labels and use the truck scanning/receiving path. Sales, pickups, order payments, refunds, vouchers and truck scans are dated on weekdays during 10:00–18:00 opening hours. Transactions occupy the requested calendar-day window through yesterday, so a load never invents later transactions today. There are no public-holiday exclusions.

All monetary documents go through `sales.services.save_document`, order payment/pickup services, and purchasing services. Catalog rows alone use bulk insertion. Dates and source tags are applied to the service-created rows; amounts, tax locks, balances and stock movements are not manufactured afterward. Daily Totals and Sales Tax use the ordinary shared ledger.

Given the same seed, window, load date and tax configuration, document amounts and tender breakdowns repeat. Database primary keys, document counters and randomly allocated piece serials are not reset or promised to repeat. Dates move with the load date.

## Guards and reset

A second load refuses an existing catalog or history. `--force` explicitly appends another batch with distinct `demo-…` source IDs; it does not overwrite catalog rows. Unless `--preserve-settings` is also given, it configures the demo business and tax settings. Use this only on an intentional test copy.

`--reset-demo` attempts to delete demo-owned catalog/document rows and reload in one transaction. Roots with `source_id` use the reserved `demo-` prefix; child rows without a source field inherit ownership from their tagged parent. Shared business/tax settings, discount reasons, logins and document-number counters are retained.

Reset refuses mixed demo/untagged catalog or document data. It also refuses any truck sessions, inventory adjustments or items in trash: these models have no safe demo source tag. **A history load contains truck sessions, so `--reset-demo` deliberately refuses it without deleting anything.** Choose a new test database filename to rebuild full history. This command never flushes the database or guesses ownership from a display name or free-text note.

The original PNG can be regenerated with `python shop_settings/fixtures/generate_demo_logo.py`; it uses Pillow, bundled font files and no copied artwork.
