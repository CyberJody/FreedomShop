# FreedomShop · Point of Sale

FreedomShop is a browser-based point of sale and inventory application for independent shops. Run it on your own server to handle the counter, customer accounts, purchasing, stock, printing and daily reports. Business details and sales tax rules belong to your installation. [FreedomBooks](https://freedombooks.app) is its accounting sibling.

## Recommended deployment

**Run FreedomShop on a trusted local network. For remote access, use a private VPN such as Tailscale, restricted to authorized users and devices. Direct public-internet exposure is not recommended.**

The current testing does not establish readiness for public-internet hosting. A hosted server should follow the same private-access model; an internet-facing hosting service would need a separate security and operations assessment. See [INSTALL.md](INSTALL.md#network-access) for network and HTTPS guidance.

## Who it is for

For shops moving from QuickBooks POS and teams that need sales orders, deposits and piece-by-piece receiving alongside everyday sales. QuickBooks POS is named only to identify the migration source; FreedomShop is an independent project, with no affiliation or endorsement implied.

## Features

- Sales, returns, held receipts and layaways through sales orders and deposits.
- Sales orders, partial pickups, deposits and refunds.
- Customers, charge accounts, account limits and payments on account.
- Items, departments, vendors and reorder reminders.
- Purchase orders, receiving vouchers and truck receiving by piece serial.
- Receipt printing, Letter documents, Zebra labels and furniture set tags.
- Daily Totals, Sales Tax, General Sales, Sales by Vendor, Inventory On Hand and Customer Balances reports, with PDF and spreadsheet exports.
- FreedomBooks exchange: review daily summaries and checks; download JSON files or a date-range ZIP.
- First-run setup wizard and `manage.py setup` for a terminal.
- Business details, receipt text, logos, tax locations, categories and filing periods.
- An in-shop **Notes & Requests** notebook.

Payment methods record how payment happened; FreedomShop does not process cards. The current FreedomBooks exchange creates files and proposals for review. It does not post to accounting automatically.

## Screenshots

Capture these six screens from a test installation using **Demo Home & Hardware** and generated records. These are planned image paths; screenshots are not included yet.

| Screen to capture | Placeholder |
| --- | --- |
| Navigator | `docs/screenshots/navigator.png` |
| Make a Sale | `docs/screenshots/make-a-sale.png` |
| Sales order with deposits and pickup quantities | `docs/screenshots/sales-order.png` |
| Truck Receiving with scanned piece serials | `docs/screenshots/truck-receiving.png` |
| Daily Totals report | `docs/screenshots/daily-totals.png` |
| Tax Settings | `docs/screenshots/tax-settings.png` |

## Quick start

On Debian 13 or Ubuntu 24.04, extract a release archive and run:

```sh
sudo ./install.sh
```

The installer prepares the application user, directories, Python environment, database, systemd service and nginx. Open the installation in a browser and finish `/setup/`.

For the optional container route, copy `.env.example` to `.env` and configure it as described in [INSTALL.md](INSTALL.md), then run:

```sh
docker compose up -d --build
```

FreedomShop is a server application: there are no Windows or Mac desktop builds. Each register opens the shop's address in a web browser. Releases also provide a Debian/Ubuntu `.deb` package and a container image at `ghcr.io/cyberjody/freedomshop`. Until the first release is published, run `sudo ./install.sh` from a clone of this repository.

See [INSTALL.md](INSTALL.md) for prerequisites, configuration, HTTPS, printers, upgrades and backups.

## Do it yourself, or get help

FreedomShop is free software. You can install it, run it and change it yourself: [INSTALL.md](INSTALL.md) covers the server, printers, HTTPS, upgrades and backups, and [GitHub Issues](https://github.com/CyberJody/FreedomShop/issues) and [Discussions](https://github.com/CyberJody/FreedomShop/discussions) are open to everyone.

If you would rather have help, [TechQuest](https://techquest.guru) offers paid support, deployments and add-ons. It is optional; FreedomShop works the same without it.

## Status and help

**Early beta.** Bugs and unfinished features are expected. Try a separate test installation before using real records. [Report a problem on GitHub](https://github.com/CyberJody/FreedomShop/issues), including your version and steps to reproduce it with sample data. Read the [User Guide](docs/USER-GUIDE.md), [exchange format](docs/EXCHANGE.md) and [changelog](CHANGELOG.md).

The project home is [CyberJody/FreedomShop](https://github.com/CyberJody/FreedomShop) and the website is [freedomshop.app](https://freedomshop.app). [GitHub Issues](https://github.com/CyberJody/FreedomShop/issues) is the public project channel. Notes & Requests stays inside your shop; it does not submit GitHub issues. **File → About FreedomShop** shows the installed version, the license and these links.

## License, contributions and security

Copyright © 2026 Jody Grafals.

FreedomShop is licensed under **AGPL-3.0-or-later**. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [third-party notices](THIRD-PARTY-NOTICES.md).

Contributions are accepted under the [Contributor License Agreement](CLA.md); a bot asks you to sign it once on your first pull request. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

Report vulnerabilities privately using the process in [SECURITY.md](SECURITY.md).
