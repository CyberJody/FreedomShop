# Changelog

Notable changes to FreedomShop · Point of Sale are recorded here. This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project is pre-release; no numbered release has been published.

## [Unreleased]

### Added

- Register workflows: sales, split tenders, returns, discounts, gift certificates, held receipts and pickup tracking.
- Sales orders and layaways using deposits, refunds and partial pickups.
- Customers, charge accounts, limits, credits and payments on account.
- Inventory items, departments, vendors, available-stock details and reorder reminders.
- Purchase orders, receiving vouchers, reversal and truck receiving by piece serial.
- ESC/POS receipts, Letter printing, Zebra item tags, furniture set tags and PO piece labels.
- Daily Totals, Sales Tax, General Sales, Sales by Vendor, Inventory On Hand and Customer Balances reports, with PDF and spreadsheet exports.
- Configurable business details, logos, receipt policies, copy defaults and serial prefixes.
- Sales tax locations, categories, default rates and configurable filing periods.
- Six-step setup wizard and terminal setup, including optional demo records and testing marks.
- FreedomBooks daily exchange files, range ZIP downloads, source checks, stable shop identity and terminal export.
- In-shop Notes & Requests and links to the project issue tracker.
- Installation and user guides, Contributor License Agreement (CLA) with a signing check, security policy, Code of Conduct and dependency notices.
- About page with the version, license, credits and where to find free or paid help.
- Debian/Ubuntu package and container image builds in the manual release workflow.

### Changed

- Product identity is FreedomShop, with Point of Sale as its subtitle.
- Business names on shop documents come from Business Settings.
- Installation settings and test examples use generic configuration and generated data.

### Fixed

- Historical payment-tax amounts stay stable when current tax rates change.
- Account-charge corrections keep the original tax jurisdiction, including partial payments.
- Referenced tax locations and categories cannot be deleted; retirement preserves history.
- Existing database imports require a tax-year choice when it is unknown.
- Tax-category migration preserves original strings, settings caches respect transaction rollback, and long location names fit payment locks.

### Removed

- Private upstream history from the public working tree.

[Unreleased]: https://github.com/CyberJody/FreedomShop
