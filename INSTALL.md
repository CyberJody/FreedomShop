# Install FreedomShop

FreedomShop is a pre-release. Native Debian 13 installation is the first path; Ubuntu 24.04 is also a packaging target. A `.deb` package and Compose are alternatives. Until the first release is published, run the installer from a clone of the repository instead of a release archive.

## Network access

**Recommended usage is a trusted shop LAN, with remote access through a private VPN such as Tailscale. Direct public-internet exposure is not recommended.** This also applies to a server hosted outside the shop: keep access private and limited to authorized users and devices.

Before installing on a machine with a public network interface, configure its firewall to allow FreedomShop web access only from the intended LAN or VPN. Do not make the application reachable through public router port forwarding, a public reverse proxy or a public tunnel. With Tailscale, use private tailnet access or [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve), with access rules for the intended staff and devices. [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel) makes a service publicly accessible and does not meet this recommendation.

Use a stable private address or DNS name for the server. A name such as `shop.example.com` in this guide can resolve only on your LAN or VPN; using a domain name does not require public access. Restrict guest Wi-Fi and other untrusted networks from reaching the service. A private network does not replace application logins, permissions, updates, backups or [HTTPS](#https).

## A server, with browsers at the registers

FreedomShop is a server application. Install it once, on a Linux server in the shop or one you control; there are no Windows or Mac desktop builds. Each register, office computer or tablet opens the shop's address (for example `https://shop.example.com/`) in a current web browser, so Windows, Mac, Linux and ChromeOS registers all work without installing anything.

To make a register show only FreedomShop, run the browser full screen:

- **Chrome or Chromium:** start it with `--kiosk https://shop.example.com/` (on Windows, add this to a shortcut's target). Press Alt+F4 to leave.
- **Microsoft Edge:** `msedge --kiosk https://shop.example.com/ --edge-kiosk-type=fullscreen`, or use Windows' assigned-access kiosk setting.
- **Safari on a Mac:** open the address, then **View → Enter Full Screen** (Control-Command-F). For a locked-down register, use Chrome's `--kiosk` on the Mac instead.

## Do it yourself, or get help

FreedomShop is free software, and everything in this guide can be done yourself. Questions go to [GitHub Issues](https://github.com/CyberJody/FreedomShop/issues) or [Discussions](https://github.com/CyberJody/FreedomShop/discussions). If you would rather have someone else do it, [TechQuest](https://techquest.guru) offers paid support, deployments and add-ons; it is optional.

## Requirements

Use a dedicated Debian 13 or Ubuntu 24.04 server, a sudo-capable account, network access for system and Python packages, and enough disk space for the database, uploads and backups. You also need a browser on each register and a stable server address or hostname.

The native stack is Python 3.12 or newer in a virtual environment, Django 5.2, PostgreSQL, gunicorn, systemd and nginx. The installer provisions the stack. Keep printer connections reachable from the server. Docker Engine and the Compose plugin are needed only for the container route.

## Native installation

Download the release archive and its checksum from [GitHub Releases](https://github.com/CyberJody/FreedomShop/releases). Verify the checksum before extracting it. From the extracted directory, run:

```sh
sudo ./install.sh
```

The agreed installer interface creates the `freedomshop` system user, application and data directories, virtual environment, PostgreSQL role and database, systemd service and nginx site. Releases live under `/opt/freedomshop/releases/`; `/opt/freedomshop/current` selects the active one. Installation settings live in `/etc/freedomshop/settings_site.py`; persistent data lives in `/var/lib/freedomshop`. Keep credentials out of the source tree.

Review the generated settings and nginx site for the server's hostname, allowed hosts, database connection and HTTPS arrangement. Use the paths and service instructions printed by the installer; do not substitute development settings on a live installation.

## First run

Open `/setup/` on the installed server. Restrict access to the installation until the first administrator is created. The wizard collects business details, sales tax, an admin login, an optional shared register login and PIN cashiers, an optional receipt printer, optional demo records, and the Live shop choice. Leave Live shop unticked while testing; printouts carry testing bars.

Demo loading is for an empty shop: it refuses existing customers, items, departments or vendors. To use the demonstration name, enter **Demo Home & Hardware** in the business step. Loading demo records in the wizard preserves the business details you entered.

For terminal setup, the packaging provides a wrapper for `manage.py setup` with the installed environment and service account:

```sh
sudo freedomshop manage setup
sudo freedomshop manage setup --help
```

Interactive setup prompts for secrets without displaying them. The help describes unattended flags; passwords and PINs are read from named environment variables, never literal password arguments. `--force` reruns setup, changing only supplied values; it skips login creation when an admin already exists.

An admin can use **Business Settings → Run setup again** later. Time zone, currency and Live shop are currently changed there. Restart the application after changing the time zone. Currency is stored, but printed amounts still use the dollar symbol.

## Debian package alternative

Releases include `freedomshop_VERSION_all.deb` for Debian 13 and Ubuntu 24.04. It declares the same system packages the installer would add, and its setup step runs the same installation as `install.sh` (or the same steps as `freedomshop upgrade` when FreedomShop is already installed). Give the shop's host names on the first install:

```sh
sudo FREEDOMSHOP_ALLOWED_HOSTS=shop.example.com apt install ./freedomshop_VERSION_all.deb
```

Without `FREEDOMSHOP_ALLOWED_HOSTS` the server's host name is used; change it later in `/etc/freedomshop/settings_site.py`. Removing the package leaves the installation, its database and its backups in place.

## Compose alternative

Use `docker-compose.yml` and `.env.example` from the same release. Releases also publish a ready-built image, `ghcr.io/cyberjody/freedomshop:VERSION`; to use it instead of building locally, replace `build: .` under `app` with `image: ghcr.io/cyberjody/freedomshop:VERSION`. Copy `.env.example` to `.env`, set independent values for `FREEDOMSHOP_SECRET_KEY` and `POSTGRES_PASSWORD`, and set `FREEDOMSHOP_ALLOWED_HOSTS` for the browser address. Review the persistent volumes and exposed ports before starting:

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

The default app address is `http://127.0.0.1:8001` on the server. To reach it from registers, configure a trusted reverse proxy reachable only on the intended LAN or VPN. `FREEDOMSHOP_BIND_ADDRESS` controls both the published app port and the optional Compose nginx port. Keep it on loopback when using a proxy on the host; otherwise bind only to the required private interface and restrict the app port to the proxy. Binding to `0.0.0.0` listens on every IPv4 interface, including any public interface, so a firewall must still restrict access.

Open the private web address and complete `/setup/`, or run `docker compose run --rm app manage.py setup`. The entrypoint runs migrations before starting the app. Keep PostgreSQL private to the server or container network. Keep persistent database and upload volumes when replacing containers; `docker compose down -v` removes volumes and is not an upgrade step.

The app container must be able to reach supported network printers. A printer at `127.0.0.1` inside a container means that container. Use a reachable printer address. Apply the HTTPS options below at the reverse proxy.

## Supported printers

The tested status in these printer and scanner lists reflects established built-in support; this documentation update includes no new hardware testing.

Printers, tested: Star Micronics TSP143IIILAN (receipts); Zebra GX420t (labels); Brother MFC-L8900CDW (letter documents, tray selection).

Printers, should work (untested): other Star TSP100-series LAN receipt printers; ZPL Zebra label printers (e.g. ZD-series); IPP Everywhere / AirPrint letter printers. Other ESC/POS receipt printers may need adjustment.

Everything else: print the PDF from the browser; USB receipt printers or custom setups — contact TechQuest for a custom solution.

The server sends direct print jobs to supported network printers. Browser Print and available PDF downloads offer a separate route through the workstation. Receipt-printer setup in the wizard is optional; Letter and Zebra printers are added on the Printers screen.

Open **File → Printers**. Add a name, kind, host and port, then save. Only send a test page when the intended printer is connected and ready.

- **Receipt:** raw TCP, usually port `9100`.
- **Letter:** IPP with the app's PWG raster output, normally port `631` at `/ipp/print`. Choose the paper tray, color option and order-copy count as needed.
- **Zebra labels:** ZPL over raw TCP, usually port `9100`. Item tags are designed for 3 × 4 inch stock.

## Supported barcode scanners

Scanners, tested: generic USB barcode scanner in keyboard (HID) mode.

Scanners, should work: any USB or Bluetooth scanner in keyboard (HID) mode that sends Enter after each scan; 2D scanners for QR codes.

Scanner notes: no driver needed; it types into the focused field.

The tested scanner supports the app's QR serial labels. Use a 2D scanner for QR codes, including on Scanner Test. The scan boxes on Truck Receiving and Scanner Test take focus automatically.

## HTTPS

Use HTTPS within the private deployment described above. A certificate and HTTPS do not establish readiness for public-internet hosting or change the private-network recommendation.

Use nginx with either a certificate from a public certificate authority, your organization's trusted local CA, or a TLS reverse proxy in front of the installation. Public certificates can use DNS validation when the server is reachable only on a private network. Install the complete certificate chain and arrange renewal and nginx reloads.

Keep gunicorn private to the server or container network. The trusted proxy must overwrite `X-Forwarded-Proto`; the application uses it to recognize HTTPS. Set the allowed hosts and trusted CSRF origins to the actual browser address. Enable secure session and CSRF cookies and HTTPS redirects for the live site. Start HSTS only after HTTPS and renewal work reliably.

If the installer offers HTTP for initial local setup, keep that access restricted and move to HTTPS before staff enter live credentials or customer records. The base application settings expect HTTPS; any temporary HTTP mode must be configured explicitly by the packaging.

## Upgrade and rollback

Schedule downtime and keep the old release archive. Check the new changelog and verify its checksum, then run:

```sh
sudo freedomshop backup
sudo freedomshop upgrade /path/to/freedomshop-VERSION.tar.gz
```

Replace `VERSION` with the release version and keep its `SHA256SUMS` beside the tarball. The upgrade command verifies the archive, takes a backup, applies migrations and checks the service. If the upgrade fails after database changes, it restores the pre-upgrade database and files. Read its output and confirm the installed version, `/health/`, sign-in, settings and a test document afterward. Do not test by printing to a live printer unexpectedly.

A database migration may make old code incompatible. Rollback means restoring the matching pre-upgrade database, application version, site settings and uploaded files together. Reinstalling an older archive alone is not a safe database rollback. Pause writes until validation is complete; a restore discards later changes.

For Compose, back up the database, uploads and deployment configuration first. Rebuild or replace the app from one release, run its documented migrations and confirm health before reopening access. Keep the previous image and matching backup for recovery.

## Backup and restore

Administrators can use **File → Backups** (`/settings/backups/`) to back up now, download a backup, set the nightly schedule and retention, and restore. [docs/BACKUPS.md](docs/BACKUPS.md) describes all of it. On native installs the same core is available from the shell:

```sh
sudo freedomshop backup [--keep N]
```

This runs the app's `manage.py backup` as the `freedomshop` user, verifies the result and prints its folder, `/var/lib/freedomshop/backups/YYYYMMDD-HHMMSS/`. The folder contains `db.dump` (`pg_dump -Fc`), `media.tar.gz` (logos and Notes & Requests uploads), `settings.tar.gz` (`settings_site.py` and the secret key), `MANIFEST.json` and `SHA256SUMS`. `--keep N` afterwards keeps only the newest N verified scheduled backups. The installed `freedomshop-backup.timer` checks every five minutes whether the Backups page schedule is due. On a new native install, backups run nightly at 02:00 and keep 14 scheduled backups. Manual and safety backups are never deleted automatically. Keep any separately customized nginx configuration and certificates too.

Store an encrypted copy off the server and rehearse recovery on a separate installation. Preserve the database's shop exchange identity when restoring or moving the same shop. To restore a backup folder (one under the backups folder, or one copied from elsewhere, which is copied in first), run:

```sh
sudo freedomshop restore /path/to/YYYYMMDD-HHMMSS
```

The restore uses the installed release; it does not install other code, so `--wheelhouse` is accepted but not needed. It verifies the backup and restores it into a new database copy, checking it while the live shop keeps running. It then switches over: the shop shows a maintenance page, a fresh safety backup must verify, and the database, uploads and changed settings are swapped in. On any failure the live data stays as it was and the command says where it stopped. A backup from a different application version (other migrations) is refused; restore it with the matching release. Settings files the app cannot write (`/etc/freedomshop`) are kept as they are and listed in the report. The service restarts afterwards so every worker loads the restored data. Recovery replaces current records with the selected snapshot, so later changes are lost.

`freedomshop upgrade` still takes its own rollback snapshot (`before-VERSION-…`, including the release code) so a failed upgrade can return to the previous release and database together.

After recovery, check `/health/`, sign-in, business and tax settings, document totals, uploads and the exchange shop identity. Reopen access only after those checks succeed. On Compose, use the Backups page or `docker compose exec app python manage.py backup` (and `restore_copy` / `switch_over`); backups live in the `app-data` volume, so copy them off the server too.
