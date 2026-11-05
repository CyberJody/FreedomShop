# FreedomBooks exchange — freedomshop-exchange/1 final

FreedomShop exports one `freedomshop-exchange/1` JSON file per business day. An administrator can open
**Reports → FreedomBooks exchange**, choose a day or inclusive range, inspect the cash-basis summary,
checks and command proposals, and download a day as JSON or the range as a ZIP. Ranges are limited to
366 days. The download URLs are `/reports/exchange/YYYY-MM-DD.json` and
`/reports/exchange/?start=YYYY-MM-DD&end=YYYY-MM-DD&format=zip`.

The exporter reads the existing cash-basis ledger used by Daily Totals and Sales Tax. It also reads
completed pickup receipts for deposit applications and cost, and receiving vouchers posted that day.
It does not change documents, tax locks, ledger calculations or accounts. Local exports and acknowledgement
collection make no network calls. Only the explicit `--push` flag contacts the configured receiver. The first Business Settings read may create the normal settings row and its permanent
shop identity. Exporting requires that identity to have been saved successfully.

## Command line

Run in the application's environment, with its usual settings and database configuration:

```sh
python manage.py export_exchange --day 2026-11-03 --out /path/to/exchange
python manage.py export_exchange --from 2026-11-01 --to 2026-11-03 --out /path/to/exchange
```

Set **Settings → Business → FreedomBooks exchange → Exchange outbox** to an absolute directory.
`--out DIR` overrides it for one invocation. Set the local closing time there (default **23:59**).
The business day is a calendar day in the active Django time zone, exactly as in the reports; closing
time determines eligibility and does not move the midnight boundary. Future/open days cannot be exported.

```sh
python manage.py export_exchange --pending
python manage.py export_exchange --collect-acks
python manage.py export_exchange --pending --collect-acks
python manage.py validate_exchange docs/exchange-examples/*.json
```

`--pending` walks from the earliest dated source/run/correction through the last closed day, including
empty gaps, skipping published exports. With no dated data or runs it does nothing. It has no 366-day
backlog limit. An initial catch-up can take time because it builds every missing calendar day. The
scheduler never pushes HTTP. Examples `deploy/freedomshop-exchange.service` and
`deploy/freedomshop-exchange.timer` run nightly at **02:30 in the system time zone** with missed-run
catch-up. Match that zone to Django, adjust paths/user/outbox permissions, then enable manually.
Nothing is installed by this feature. The example grants writes under `/var/lib/freedomshop` only.
Protect the outbox and backups: exports include account-customer contact data and financial records.

`ExchangeRun` has a unique day, generated timestamp, SHA-256 of the **whole file**, pending/exported/
acknowledged/failed status, acknowledgement reference and error. It also retains the exact bytes,
raw source snapshot, publication timestamps and draft push responses. The database snapshot commits
before publication; interrupted publication is recoverable using those retained bytes. Writes are
atomic. Exported files are never regenerated, even after contact/settings edits. A conflicting outbox
file is refused; move it aside and re-export. Failed-check drafts are inspectable but can be rebuilt
until their first successful export. Failures return a nonzero command exit; other local days continue.
Keep database backups together with outbox files and the permanent shop identity.

JSON/ZIP downloads retain closed days too. Page display alone is a preview. The latest 100 runs appear
on `/reports/exchange/`; failures and the receiver's escaped error text are red. The CSRF-protected,
admin-only **Re-export** POST downloads the retained bytes and restores the configured outbox file.
It preserves every byte, timestamp, checksum and request key. It does not acknowledge an import or
clear acknowledgement/push failures. If no outbox is configured, browser downloads still retain the
artifact in the database. Use a range/day command or Re-export to copy such artifacts to an outbox.

## Acknowledgements

FreedomBooks writes `YYYY-MM-DD.ack.json` into the same directory, atomically, after import:

```json
{"day":"2026-11-03","imported_at":"2026-11-04T02:35:00-05:00","request_keys_applied":[],"errors":[]}
```

The four fields are required, with no extras. `imported_at` must include a time zone. Applied keys
must be unique canonical UUID strings belonging to that retained day's commands. A successful ack
contains **every non-optional command key**; optional bank proposals may be omitted. An empty day's
key list is empty. `errors` is an array of receiver messages or structured error objects; any error,
missing required key, unknown key, malformed file or mismatched day marks the run failed. Other ack
files are still collected. Fix the receiver issue and atomically replace the ack to retry collection.
The acknowledgement reference includes its filename and SHA-256. Files larger than 1 MiB are refused.
Acknowledgements are trusted local input, not signed proof of import; directory access controls matter.

## File format and identity

The envelope has exactly three fields:

| Field | Meaning |
| --- | --- |
| `body` | The deterministic exchange document, containing `format`, `shop`, `day`, `source`, `summary`, `day_summary`, `corrections`, `directory`, `commands`, `references`, `ledger`, `checks`, `diagnostics`, and `requires`. |
| `body_sha256` | Lowercase SHA-256 of the canonical UTF-8 bytes of `body`. This is an integrity checksum, not a cryptographic signature or proof of origin. |
| `generated_at` | An ISO timestamp outside the checksum body. Previews may vary here; a retained export freezes this field too. |

`body.format` is `freedomshop-exchange/1`. `shop` contains the saved Business `exchange_id` UUID,
name, currency (`SHOP_CURRENCY`, default `USD`) and active Django time zone. The business day uses
the same local midnight boundaries as the reports. `source` is
`{"app":"freedomshop","version":"1","basis":"cash"}`; version `1` identifies this producer contract.

All amounts are signed decimal strings with exactly two fractional digits. Quantities and rates in
command values are strings. Dates use `YYYY-MM-DD`. Counts and booleans retain their JSON types.
There are no binary floating-point amounts. Empty days have zero summaries, empty commands and
directory lists, and passing checks.

Canonical JSON sorts object keys, uses no optional whitespace, writes Unicode directly in UTF-8,
and rejects NaN/Infinity. The downloaded envelope has one trailing newline. The checksum excludes
that newline and the envelope. ZIP entries have fixed metadata and chronological filenames; their
JSON envelopes still contain `generated_at`.

Business migration `0007_business_exchange_id` backfills a distinct UUID for every existing Business
and supplies a UUID once for new rows. Changing the shop name or address does not change it. Preserve
it in backups and when moving the same shop to another installation; a separately operating shop
needs its own identity. The settings form does not expose this field.

Each proposal uses zero-based ordinals within its `section`:

```text
request_key = UUIDv5(namespace = shop.exchange_id,
                     name = "YYYY-MM-DD:section:ordinal")
```

Ordering is explicit: ledger event time, document/payment kind and primary key; customer/vendor
primary key; receipt tender/location/sign groups; voucher time and primary key; department name.
Money allocations use cumulative half-up rounding with the final residual cent retained. Repeating
an export with unchanged data produces identical bytes after removing only `generated_at`, including
the checksum and every request key.

**Version 1 is frozen.** The authoritative schema is
[`reports/exchange_schema.json`](../reports/exchange_schema.json) (Draft 2020-12), with three generated,
fictional examples in [`docs/exchange-examples/`](exchange-examples/): a small sale day, a day with
returns/deposits, and an empty day. `validate_exchange` uses a small offline validator for precisely
the vocabulary in this schema, verifies the checksum, request-key consistency, summary deltas and
ledger totals, and requires passing checks. It needs no added runtime dependency. Tests also use
the pre-existing `requirements-test.txt` JSON Schema validator. A changed imported body under the
same shop/day is a conflict; never silently accept it based on matching keys. Incompatible future
changes need a new format version.

## Corrections

Cancelling a native receipt/order runs an exchange hook **inside the existing POS transaction**.
For exported historical days, it compares the current ledger snapshot to the last effective snapshot
and stores signed deltas dated to the actual local cancellation day. It scans exported days because
removing an account charge can change subsequent payment allocations. Rollback also removes the
correction. No ledger/tax math is modified. There is no file or network I/O in checkout.

`day_summary` preserves the unadjusted report figures for the file's own day. `summary` is that object
plus every `corrections[].summary_delta`. Each correction identifies `source_day`, `source`, and
`source_file_sha256` (the original whole-file digest). The `ledger` also includes negative prior rows
and positive replacement rows. Thus summing retained summaries across all affected days ties to the
current ledger, without rewriting any old file. Count deltas such as `missing_cost_lines` may be signed.
Checks establish agreement of the raw day and the before/after historical snapshots; a correction
day's combined summary can differ from the report for that single day. Compare the full affected range.

A daily sales receipt may combine several POS receipts. To avoid reversing the wrong amount, a
historical snapshot correction reverses its changed financial proposals and their dependents in reverse
dependency order, then reissues their remaining amounts with new keys on the correction date. Unchanged
proposals keep their recorded key bindings, including unrelated vouchers. Sections are
`correction:<persisted correction id>:<original section>` and `correction:<id>:reverse`; keys still use
the same UUIDv5 formula. Repeated corrections reference the previous replacements, not the original
keys again. Directory saves reuse their stable record bindings.

- Exported sale receipt cancellation: `ReverseCustomerReceipt`; remaining aggregate sales are reissued.
- A rescinded exported deposit receipt: `VoidCustomerDeposit`.
- An order cancellation that actually refunds cash/card/account: the ledger's new negative payment
  produces `ReviewCustomerRefund` → `RecordCustomerRefund` with `deposit: true` on the refund day.
  Do not also void that deposit: that would reverse the same cash twice.
- An ordinary linked merchandise return remains a new dated credit memo/refund, with no historical
  rewrite. Related COGS/gift journals and optional bank-deposit proposals reverse/reissue as needed.

Inverse entries carry `target_request_key`; IDs inside their `values` are **binding keys**, and
`expected_revision: 0` is a binding placeholder. The receiver must resolve the recorded ID and current
revision before invoking Rust. Skip an unrecorded optional proposal. Correction command fields are
grouped under `values` and flattened by the adapter. Respect Review/Record steps on replacements.
The receiver must not post both the combined summary delta and these detailed proposals.

Cancellation after the current calendar day's export is refused transactionally, because that day
has already closed and its file cannot be changed. Finish POS activity before exporting a day.
This hook covers the supported cancellation service and ordinary dated returns/refunds; direct SQL,
source rebuilds and arbitrary historical edits outside that workflow require manual reconciliation.

## Summary and checks

`gross_sales`, `discounts`, `taxable`, `nontaxable`, `tax_collected`, `total` and `by_tax_location`
mirror Sales Tax, including signed returns, deposits and paid account allocations. In particular:

```text
gross_sales - discounts = taxable + nontaxable
gross_sales - discounts + sum(tax_collected) = total
total + unlinked_deposits = received + tenders.gift
received = tenders.cash + tenders.check + tenders.card
```

The third identity is checked against Daily Totals. Unlinked deposits are included in Received there
but excluded from Sales Tax because their tax is unknown. Missing or inconsistent source amounts can
fail the checks; the exporter preserves the recorded figures and diagnostics for review.

`tenders` uses the shared ledger's method buckets: cash, check, card (credit and debit together),
account, and gift (gift and other tenders together). Received excludes account and gift/other.
`gift_certificates_redeemed` identifies actual gift-method tenders, including a single missing
imported amount inferred by the ledger; `other_tenders` is the rest of the gift/other bucket.
Only actual gifts generate a gift-liability journal.

`returns` is the positive magnitude of cash-basis negative merchandise extensions **before receipt
discount and tax**. Signed returns are already in gross; do not subtract this reference field again.
`restocking_fees` and `gift_certificates_sold` are also pre-discount cash-basis extensions. The
`daily_totals` and `daily_sales_summary` objects preserve the existing Daily Totals columns and its
distinct gross/returns presentation, including tax and new orders. They need not equal the cash-basis
Sales Tax gross because their recognition timing differs.

`order_deposits_taken` is net deposits less refunds, including account tenders. Account tender is a
charge, not money received. `order_deposits_applied` is the total of native pickup receipts that day;
it creates no second cash-basis sale. `legacy_deposits_excluded` preserves the ledger's excluded
legacy deposit amount. `payments_on_account` includes signed refunds and the payment portion of
mixed merchandise/payment receipts. Account credit applications can move tax between locations
without money moving; their signed rows appear in `ledger` and `diagnostics.tax_adjustments`.

`cogs` uses recorded `Line.unit_cost` on sold merchandise and completed pickups. It reverses cost
for sellable returns, keeps damaged-return cost expensed, and excludes gifts, payments on account,
restocking fees and known noninventory items. Unknown recorded costs contribute zero and increment
`missing_cost_lines`; no current average cost is substituted. `inventory_received_at_cost` is posted
voucher total, including recorded freight/residuals; bills identify those separately from item costs.

| Check | What a pass establishes |
| --- | --- |
| `tenders_equal_total` | No missing-tender event, and each included receipt/payment's signed tender sum equals its recorded total/amount. This includes account tender. Excluded payouts/pickups/legacy-applied deposits follow the existing ledger rules. |
| `ledger_ties_daily_totals` | The independent exchange total plus unlinked deposits equals Received plus Gift/Other; Received, tax and every tender bucket agree with Daily Totals. |
| `ledger_ties_sales_tax` | All six fiscal columns and each tax-location bucket agree with a separately built Sales Tax report. |

Passing these checks establishes source/report agreement. It does not establish that a destination
book is mapped correctly or that proposed commands can be posted unchanged. Diagnostics include
excluded/derived/missing counts and estimated payment amounts/tax, with the ledger's own estimate.

## Command proposals and directory

Each `commands` entry contains `section`, `type`, `request_key`, `values`, `fs_source` document
numbers, and `next_types`. A Review/Record pair is **one entry**, holding the Review values and the
Record type in `next_types`. Bills similarly use one `SaveBill` entry with `ReviewBill`, `PostBill`;
journals use one `SaveJournal` entry with `PostJournal`. No review tokens are fabricated.

`values` follows the current FreedomBooks v2 Rust struct fields. The receipt discriminator
`sales_receipt`, refund/application `source_id`, refund `deposit` flag and directory `record_id` are
siblings of `values`, matching their location in the Rust command. `SaveJournal` has no separate
Rust Values struct: its command fields are grouped under `values` for the exchange and must be
flattened by the importer. The chosen destination `company_id` is deliberately absent.

IDs in these proposals are local references, not existing destination records. `references` maps
stable shop-scoped UUIDs to a kind, source key and label. An importer must persist bindings for
customers, vendors, department items, accounts, banks, tax items, pickup invoices and deposit/credit
lots. Directory `record_id` is a binding key: use a bound destination ID for updates, or `null` when
creating, then remember the returned ID. A request key used as `receipt_ids` or a refund `source_id`
means the recorded result of the proposal with that key. Never submit those UUIDs as assumed book IDs.

`directory.mode` is `summary`: only customers with account charges, payments, credits, true-ups or
order-deposit activity, and vendors with vouchers posted that day, are included. The entries also
appear in `commands`. Ordinary retail customer details are omitted; sales use a synthetic
“Daily sales — walk-in” reference. Full customer sync is not offered. Department-as-item directory
proposals appear in `commands` with `NONSTOCK` type, a stable code and mapped income account.

## Mapping table

This implements the release plan §6.4; the correction rules above extend these mappings.

| POS event (source) | FreedomBooks proposal | Implemented behavior / dependency |
| --- | --- | --- |
| Day's sales by tax category/location (`tax_ledger` and tenders) | `ReviewCustomerReceipt` / `RecordCustomerReceipt`, `sales_receipt: true` | One receipt per tender bucket, tax location and sign; department income lines and explicit taxable flags; walk-in customer; `UNDEPOSITED` destination. Cash-basis shares exclude unpaid account portions and gift issues. |
| Sales tax collected | Receipt `tax_mode: ITEM`, `tax_item_id`; sibling `expected_tax` | Stated POS tax is authoritative. Values have no invented `tax_amount` field. Aggregate receipt tax can differ from recomputation; stated-tax support is required. All locations, order tax, account allocations and zero-money true-ups remain explicit in `ledger`. |
| Cash/check/card tenders → bank | `ReviewDeposit` / `RecordDeposit` with `receipt_ids` | Optional proposals group positive cash/check/card sales, order deposits and account-payment receipts. Bind recorded IDs and banks; reconcile gifts sold, refunds, fees and actual settlements before selecting receipts. Journal-only gift issues are not receipt IDs. |
| Deposits taken on sales orders (`OrderPayment` and linked imported deposits) | `ReviewCustomerDeposit` / `RecordCustomerDeposit` | Per tender/event, order number in `order_reference`; liability proposal. Negative payments use deposit refunds. Account-paid deposits need receivable mapping. Resolve the POS cash-basis tax versus a full liability posting before import. |
| Deposit applied at pickup | `ReviewDepositApplication` / `RecordDepositApplication` | Native pickup total, source order and pickup-invoice references. Requires destination invoice and deposit-lot allocation; no duplicate sale or tax. Legacy exclusions stay in the summary/diagnostics. |
| Payments on account (`Payment On Account` lines) | `ReviewCustomerReceipt` / `RecordCustomerReceipt`, `sales_receipt: false` | Actual tender shares, with allocations left empty in summary mode. Bind historical invoices/credits first; unmatched prepayments and cash-basis tax estimates are ledger context. Negative payment shares use `ReviewCustomerRefund` / `RecordCustomerRefund` with an unresolved credit source. |
| Returns / refunds (reversals and restocking fees) | `ReviewCreditMemo` / `RecordCreditMemo`, then `ReviewCustomerRefund` / `RecordCustomerRefund` | Negative cash-basis groups become positive credit-memo/refund values. Exchanges can retain negative department lines within a net-positive receipt. Restocking fees remain income lines in the department summary. Account-only credit tax changes remain in the ledger. |
| Discounts (`Document.discount`) | Negative receipt lines to a Discounts given income reference | Taxable/non-taxable shares and residual cents are preserved. No invented discount field on `InvoiceLineValues`. |
| Gift certificates sold / redeemed | `SaveJournal` → `PostJournal` | Sold: Dr Undeposited funds / Cr Gift certificates outstanding, removed from receipt income. Redeemed: Dr Gift certificates outstanding / Cr Undeposited funds, paired with the gift-tender receipt. Negative values reverse direction. Other tenders do not generate gift journals. |
| Vendor receiving (posted vouchers) | `SaveBill` → `ReviewBill` → `PostBill` | One bill per posted voucher, amount-preserving line extensions to Inventory; freight/recorded residual to Receiving adjustments. Uses recorded invoice/due dates, or business day when absent; confirm those dates and bindings. |
| Inventory value and COGS (`Line.unit_cost`) | `SaveJournal` → `PostJournal`; receiving bills | Dr COGS / Cr Inventory for known recorded cost, reversed for sellable returns; receiving bills Dr Inventory / Cr AP. Missing costs are disclosed. No inventory module is added to FreedomBooks. |
| Customers | `SaveDirectoryRecord` Customer | Summary directory only, including account/deposit activity; contact and tax status fields, no full customer sync. |
| Vendors | `SaveDirectoryRecord` Vendor | Only vendors with posted vouchers that day, before their bills. Missing source vendors retain an unresolved bill reference. |
| Items | `SaveDirectoryRecord` Item | One nonstock item per used department/summary category; stable local ID and code, income account binding. Default rate zero; receipt lines carry actual extensions. |

`requires` at body and command level identifies mappings and decisions an importer must resolve.
Some rows, especially paid-account tax and tax-only corrections, cannot be faithfully posted by
simply replaying current receipt/deposit commands. The complete fiscal ledger and summary are the
authoritative exchange data while FreedomBooks implements an agreed summary-import path.

## Live push (draft)

Explicit opt-in only:

```sh
python manage.py export_exchange --push
python manage.py export_exchange --day 2026-11-03 --push
```

`Business.exchange_endpoint` must be set to an HTTPS adapter URL without embedded credentials,
query parameters or a fragment. Blank disables push, even if `--push` is passed. `--push` alone
walks retained, unacknowledged runs oldest first; day/range/pending limits its selection. The adapter
must implement this draft contract; the desktop FreedomBooks command bridge is not an HTTP endpoint.
No actual FreedomBooks network integration has been verified.

Each proposal is one POST to that exact URL, with `Content-Type: application/json` and
`Idempotency-Key: <command.request_key>`. The body contains `format`, `shop`, `day`, `body_sha256`,
`command` (the complete proposal entry), and `references`. The receiver persists a shop/key → payload/
result binding, rejects a changed payload for an existing key, binds destination IDs, reviews and
records all steps before replying. An identical retry returns the same confirmed result:

```json
{"request_key":"00000000-0000-4000-8000-000000000001","applied":true}
```

Only a 2xx JSON response confirming that exact key with `applied: true` counts as success. Conflicts,
validation errors, timeouts or unconfirmed responses fail the run and stop dependent commands/later
runs. Retrying sends the same payloads and keys, including commands previously confirmed. Timeout is
15 seconds per request; redirects are refused. Status, response text (at most 64 KiB), request key and
attempt time are retained in `push_responses` and visible to administrators. Response bodies are plain
text in the UI. A successful push leaves the run exported; only file acknowledgement marks it acknowledged.

**Security-sensitive TODO, intentionally minimal:** `cryptography` is absent from `requirements.lock`.
No `exchange_token` database/form field exists and no token is accepted or stored. Do not put secrets
in the endpoint URL. A future authenticated transport must first add reviewed SECRET_KEY-derived
Fernet encryption, key-rotation behavior and secret-redaction tests after that package is locked.
This stub sends no Authorization header; use only with a deliberately configured trusted test adapter.
No dependency was added and no production authentication is claimed.

## 6.5 Requests to FreedomBooks

Dated handoff — **2026-11-04** (POS → accounting):

1. **Confirm the final v1 envelope against your receiving schema.** The shared REQUESTS note says
   `RecordSalesSummary`, stated POS tax, the importer and `SalesTaxLiability` are being built on
   `fs-sync`, and confirms tax vocabulary. The promised receiving schema is absent from this read-only
   v2 checkout. Please consume the producer schema/examples above or provide an explicit adapter;
   do not assume the earlier flat draft from plan §6.3. `body`/checksum/metadata, signed correction
   deltas and references are authoritative. Acknowledge which detailed proposal keys a summary post
   satisfies. Never post both summary and detailed command amounts.
2. **Support dated corrections and bind recorded results/revisions.** Reverse aggregate proposals,
   reissue remaining amounts, resolve `target_request_key` and optional unrecorded proposals, retain
   key mappings across days/restarts, and accept explicit POS tax without recomputation. Confirm
   `ReverseCustomerReceipt`, `VoidCustomerDeposit` and deposit `RecordCustomerRefund` behavior,
   including multiple deposit lots, pickup applications and already-banked receipts. Refuse unresolved
   mappings instead of assuming local UUIDs or revision placeholders are book IDs.
3. **Write the four-field `YYYY-MM-DD.ack.json` contract atomically.** Return all required applied keys,
   per-command actionable error text and a time-zone-qualified import time. Re-import identical bytes
   must be a no-op. Keep partial application resumable; reject a different body for an imported day.
4. **Resolve account activity and deposits before enabling automatic import.** Your note says unmatched
   summary-mode payments on account are refused initially. Keep that refusal explicit in `errors`.
   Agree deposit tax/liability/pickup and credit-lot mapping so there is no duplicate revenue or tax.
5. **Expose range/location tax totals for a round-trip check.** Confirm the new `SalesTaxLiability`
   query and the final `CASH`/`ACCRUAL`, `MONTHLY`/`QUARTERLY`/`ANNUALLY` vocabularies in your schema.
6. **Review the HTTP adapter contract separately.** No desktop host endpoint is assumed. The draft
   requires payload-conflict checks, confirmed keys and eventual reviewed encrypted token storage;
   it is disabled until configured. File exchange remains the release transport.

FreedomBooks files were read only. The local REQUESTS pointer is the coordination channel; no messages
were sent to anyone and no receiving-side changes, scheduler installation or deployment were performed.
