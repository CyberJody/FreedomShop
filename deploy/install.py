# SPDX-License-Identifier: AGPL-3.0-or-later
"""Debian 13 / Ubuntu 24.04 native installation, invoked by install.sh."""
import argparse
import grp
import ipaddress
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .operations import Installation, log, require_root, run
    from .release import version_from_text
else:
    from operations import Installation, log, require_root, run
    from release import version_from_text

SOURCE = Path(__file__).resolve().parent.parent


def hosts(value):
    result = ["localhost", "127.0.0.1"]
    for host in value.replace(",", " ").split():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
            raise ValueError(f"Invalid host: {host!r}; supply names or addresses without schemes or ports")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if ":" in host or len(host) > 253:
                raise ValueError(f"Invalid host: {host}") from None
        if host not in result:
            result.append(host)
    return result


def render_settings(template, allowed, data, secret, https=False):
    values = {"ALLOWED_HOSTS": repr(allowed), "DATA_DIR": repr(str(data)),
              "SECRET_KEY_FILE": repr(str(secret)), "HTTPS": repr(https)}
    for name, value in values.items():
        template = template.replace(f'"@@{name}@@"', value)
    if "@@" in template:
        raise ValueError("Unfilled settings placeholder")
    compile(template, "settings_site.py", "exec")
    return template


def plan(args):
    print(f"FreedomShop {version_from_text((SOURCE / 'freedomshop/__version__.py').read_text())} installation plan")
    print("Target: Debian 13 / Ubuntu 24.04; root required except for --dry-run")
    if args.no_apt:
        print("Skip apt-get; the system packages are already installed (the .deb declares them as dependencies)")
    else:
        print("apt-get update; apt-get install python3-venv postgresql postgresql-client nginx curl ca-certificates"
              + (" libzbar0" if args.with_zbar else "")
              + (" certbot python3-certbot-nginx" if args.https else ""))
    print("Create system user/group freedomshop (no login shell)")
    print("Create /opt/freedomshop/{releases,current} and /var/lib/freedomshop/{media,backups,uploads} (owner freedomshop)")
    print("Generate /etc/freedomshop/secret_key (owner freedomshop, mode 600), preserve it on re-run")
    print("Render /etc/freedomshop/settings_site.py from deploy/settings_site.template.py; preserve existing settings")
    print(f"ALLOWED_HOSTS: {', '.join(hosts(args.allowed_hosts)) if args.allowed_hosts else 'prompt on first install'}")
    print("Create PostgreSQL role (CREATEDB, not superuser) and database freedomshop; peer authentication on /var/run/postgresql")
    print("Create per-release .venv; pip install -r requirements.lock"
          + (f" --no-index --find-links {args.wheelhouse.resolve()}" if args.wheelhouse else ""))
    print("Run migrate --plan, migrate --noinput, check, collectstatic --noinput as freedomshop")
    print("Install /usr/local/bin/freedomshop, hardened freedomshop.service and the backup timer (nightly, keep 14; set on the Backups page)")
    print("Enable nginx HTTP site for supplied LAN hosts; start services; GET /health/")
    if args.https:
        print("Obtain a certificate with certbot --nginx (HTTP-01); enable HTTPS redirects and secure cookies")
    print("Existing current release is reused on re-run; install new versions with freedomshop upgrade")


def install(args):
    require_root()
    release_info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            release_info[key] = value.strip('"')
    if (release_info.get("ID"), release_info.get("VERSION_ID")) not in {("debian", "13"), ("ubuntu", "24.04")}:
        raise ValueError("This installer supports Debian 13 and Ubuntu 24.04")
    # Native installation paths are fixed. Layout environment overrides are for CLI backups/tests only.
    for name in ("FREEDOMSHOP_ROOT", "FREEDOMSHOP_DATA_DIR", "FREEDOMSHOP_CONFIG_DIR"):
        os.environ.pop(name, None)
    site = Installation()
    allowed = None
    existing = site.config / "settings_site.py"
    if not existing.exists() or args.https:
        value = args.allowed_hosts
        if not value and sys.stdin.isatty():
            value = input("Shop DNS names / addresses (comma separated, no scheme or port): ").strip()
        if not value:
            raise ValueError("Supply --allowed-hosts HOST[,HOST...] on a noninteractive first install or with --https")
        allowed = hosts(value)
    domains = [h for h in (allowed or []) if "." in h and not is_ip(h) and h != "localhost"]
    if args.https and not domains:
        raise ValueError("--https needs a DNS domain in --allowed-hosts reachable for HTTP-01")
    if site.current.exists() and not site.current.is_symlink():
        raise ValueError(f"{site.current} must be a release symlink, not a directory")
    packages = ["python3-venv", "postgresql", "postgresql-client", "nginx", "curl", "ca-certificates"]
    if args.with_zbar:
        packages.append("libzbar0")
    if args.https:
        packages += ["certbot", "python3-certbot-nginx"]
    if not args.no_apt:
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", *packages], env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
    try:
        user = pwd.getpwnam("freedomshop")
        if user.pw_uid == 0:
            raise ValueError("freedomshop must be an unprivileged system user")
    except KeyError:
        run(["useradd", "--system", "--user-group", "--home-dir", str(site.root),
             "--shell", "/usr/sbin/nologin", "freedomshop"])
    group = grp.getgrnam("freedomshop")
    site.root.mkdir(mode=0o755, parents=True, exist_ok=True)
    site.releases.mkdir(mode=0o755, exist_ok=True)
    site.config.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(site.config, 0, group.gr_gid)
    site.config.chmod(0o750)
    site.data.mkdir(mode=0o755, parents=True, exist_ok=True)
    # The app writes its backups here and restores media/uploads by renaming them side by side.
    shutil.chown(site.data, user="freedomshop", group="freedomshop")
    for name in ("media", "uploads", "uploads/fixes", "backups"):
        folder = site.data / name
        folder.mkdir(mode=0o700 if name == "backups" else 0o750, parents=True, exist_ok=True)
        shutil.chown(folder, user="freedomshop", group="freedomshop")
    with site.lock():
        key = site.config / "secret_key"
        if not key.exists():
            with key.open("x") as stream:
                stream.write(secrets.token_urlsafe(64) + "\n")
        key.chmod(0o600)
        shutil.chown(key, user="freedomshop", group="freedomshop")
        if not existing.exists():
            existing.write_text(render_settings((SOURCE / "deploy/settings_site.template.py").read_text(),
                                                allowed, site.data, key))
        else:
            log(f"Preserving {existing}; edit this file to change the shop's hosts/settings")
        existing.chmod(0o640)
        shutil.chown(existing, user="root", group="freedomshop")
        run(["systemctl", "enable", "--now", "postgresql"])
        result = site.as_user(["psql", "--host=/var/run/postgresql", "--username=postgres", "-tAc",
                               "SELECT 1 FROM pg_roles WHERE rolname='freedomshop'"],
                              user="postgres", capture_output=True, text=True)
        if result.stdout.strip() != "1":
            site.as_user(["createuser", "--host=/var/run/postgresql", "--username=postgres", "--no-superuser",
                          "--createdb", "--no-createrole", "freedomshop"], user="postgres")
        # Restore creates a new database copy first (docs/BACKUPS.md); never superuser.
        site.as_user(["psql", "--host=/var/run/postgresql", "--username=postgres", "-c",
                      "ALTER ROLE freedomshop CREATEDB"], user="postgres", stdout=sys.stderr)
        result = site.as_user(["psql", "--host=/var/run/postgresql", "--username=postgres", "-tAc",
                               "SELECT 1 FROM pg_database WHERE datname='freedomshop'"],
                              user="postgres", capture_output=True, text=True)
        if result.stdout.strip() != "1":
            site.as_user(["createdb", "--host=/var/run/postgresql", "--username=postgres", "--owner=freedomshop",
                          "freedomshop"], user="postgres")
        if site.current.exists():
            release = site.current.resolve(strict=True)
            log(f"Reusing installed release {release}")
        else:
            version = version_from_text((SOURCE / "freedomshop/__version__.py").read_text())
            release = site.releases / version
            shutil.copytree(SOURCE, release, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", ".env", "dist", "uploads", "data"))
        site.venv(release, args.wheelhouse)
        site.manage(release, ["migrate", "--plan"])
        site.manage(release, ["migrate", "--noinput"])
        site.manage(release, ["check"])
        site.collectstatic(release)
        site.switch(release)
        helper = Path("/usr/local/lib/freedomshop")
        helper.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in ("operations.py", "release.py"):
            shutil.copyfile(release / "deploy" / name, helper / name)
            (helper / name).chmod(0o644)
        shutil.copyfile(release / "deploy/freedomshop.sh", "/usr/local/bin/freedomshop")
        Path("/usr/local/bin/freedomshop").chmod(0o755)
        for name in ("freedomshop.service", "freedomshop-backup.service", "freedomshop-backup.timer"):
            shutil.copyfile(release / "deploy" / name, Path("/etc/systemd/system") / name)
        nginx = Path("/etc/nginx/sites-available/freedomshop")
        if not nginx.exists():
            # IPv6 literals need brackets in nginx server_name.
            names = " ".join(f"[{h}]" if ":" in h else h for h in (allowed or ["localhost", "127.0.0.1"]))
            nginx.write_text((release / "deploy/nginx.conf.template").read_text().replace("@@SERVER_NAMES@@", names))
        enabled = Path("/etc/nginx/sites-enabled/freedomshop")
        if not enabled.exists():
            enabled.symlink_to(nginx)
        # Disable only the packaged default symlink, leaving other sites alone.
        default = Path("/etc/nginx/sites-enabled/default")
        if default.is_symlink():
            default.unlink()
        run(["nginx", "-t"])
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", "freedomshop.service", "freedomshop-backup.timer", "nginx"])
        run(["systemctl", "reload", "nginx"])
        if args.https:
            command = ["certbot", "--nginx", "--non-interactive", "--agree-tos", "--redirect"]
            command += ["--email", args.email] if args.email else ["--register-unsafely-without-email"]
            for domain in domains:
                command += ["-d", domain]
            run(command)
            marker = "# Managed HTTPS settings (install.sh --https)"
            content = existing.read_text().split(marker)[0].rstrip()
            existing.write_text(content + f"\n\n{marker}\nSECURE_SSL_REDIRECT = True\nSESSION_COOKIE_SECURE = True\n"
                                f"CSRF_COOKIE_SECURE = True\nSERVER_CERT_PATHS = ['/etc/letsencrypt/live/{domains[0]}/cert.pem']\n")
        run(["systemctl", "restart", "freedomshop.service"])
        site.health()
    print("FreedomShop is ready. Open /setup/ on a configured host, or run sudo freedomshop manage setup.")


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowed-hosts", metavar="HOST[,HOST...]", help="DNS names / addresses, without schemes or ports")
    parser.add_argument("--wheelhouse", type=Path, help="Offline pip wheels (apt dependencies must already be available)")
    parser.add_argument("--https", action="store_true", help="Obtain a certificate using certbot's nginx HTTP-01 plugin")
    parser.add_argument("--email", help="Certificate renewal contact for --https")
    parser.add_argument("--with-zbar", action="store_true", help="Also install optional libzbar0 for scanner checks")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without root, prompts, network or writes")
    parser.add_argument("--no-apt", action="store_true",
                        help="Do not run apt-get; for the .deb, whose dependencies are installed already")
    args = parser.parse_args()
    os.umask(0o022)
    if args.dry_run:
        plan(args)
    else:
        install(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        log(f"ERROR: {error}")
        sys.exit(1)
