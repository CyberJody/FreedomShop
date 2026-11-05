# Contributing to FreedomShop

FreedomShop · Point of Sale is licensed under AGPL-3.0-or-later. Contributions are accepted under the [Contributor License Agreement](CLA.md) (CLA). Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Issues and branches

Use [GitHub Issues](https://github.com/CyberJody/FreedomShop/issues) for project bugs and proposals. The app's Notes & Requests notebook is for each installation and does not publish issues.

Use `fix/short-description`, `feat/short-description` or `docs/short-description` branches. Keep commits focused. In the issue or pull request, use **matching** for behavior intended to reproduce QuickBooks POS and **new** for behavior introduced by FreedomShop. Describe the expected behavior and how you checked it. QuickBooks POS is a factual compatibility reference; keep it out of product branding.

## Local checks and style

Create a Python virtual environment and install the test requirements:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python manage.py test --settings=config.test_settings --noinput
python manage.py makemigrations --check --dry-run --settings=config.test_settings
git diff --check
```

The test settings use an isolated in-memory SQLite database. Print-artifact tests also require LibreOffice; the byte-for-byte comparison in `shop_settings/test_artifact_parity.py` runs only where the development history is present and is skipped otherwise. Installation and PostgreSQL checks need a separate disposable environment. Never run tests against a shop database or real printers.

Follow the surrounding Python and Django template style: four-space Python indentation, explicit names and small changes. Preserve the SPDX header, adding it after a shebang where present. Use decimal arithmetic for money, and test meaningful behavior when changing tax, totals, stock, account balances or migrations. Include a migration when models change. Use generated records in examples and tests, without customer data, private infrastructure details or credentials.

## Sign the CLA

Before your first pull request can be merged, read the [Contributor License Agreement](CLA.md). When you open the pull request, a bot comments with a link to it. To sign, reply on the pull request with this exact sentence:

```text
I have read the CLA Document and I hereby sign the CLA
```

The bot records your GitHub username and the date on the repository's `cla-signatures` branch and marks the check as passed. You sign once; it covers all your later contributions. If the check does not update, comment `recheck`. You keep the copyright in your work; the CLA grants the licenses described in it.
