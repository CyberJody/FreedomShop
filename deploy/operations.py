# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native install maintenance. Uses only the Python standard library.

Layout overrides FREEDOMSHOP_ROOT, FREEDOMSHOP_DATA_DIR and FREEDOMSHOP_CONFIG_DIR
also permit an unprivileged backup of a copied installation. Native database
name/user are freedomshop; PostgreSQL administration uses the postgres OS user.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

if __package__:
    from .release import version_from_text
else:
    from release import version_from_text

STAMP = re.compile(r"\d{8}T\d{12}Z")
BACKUP_FILES = ("app.tar.gz", "database.dump", "settings_site.py")


def log(message):
    print(message, file=sys.stderr, flush=True)


def run(command, **kwargs):
    log("+ " + " ".join(map(str, command)))
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError("Run this command with sudo (root is required).")


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_files(folder, names):
    sums = {}
    for line in (folder / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip(" *")
        if name in sums or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError("Invalid SHA256SUMS")
        sums[name] = digest.lower()
    for name in names:
        path = folder / name
        if path.is_symlink() or not path.is_file() or sums.get(name) != checksum(path):
            raise ValueError(f"SHA256 verification failed: {name}")


def extract(archive, target, roots):
    """Refuse links, devices, absolute paths and traversal before writing anything."""
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] not in roots or not (member.isdir() or member.isfile())):
                raise ValueError(f"Unsafe archive member: {member.name}")
        source.extractall(target, members=members, filter="data")


class Installation:
    def __init__(self):
        self.root = Path(os.environ.get("FREEDOMSHOP_ROOT", "/opt/freedomshop")).resolve()
        self.data = Path(os.environ.get("FREEDOMSHOP_DATA_DIR", "/var/lib/freedomshop")).resolve()
        self.config = Path(os.environ.get("FREEDOMSHOP_CONFIG_DIR", "/etc/freedomshop")).resolve()
        self.releases = self.root / "releases"
        self.current = self.root / "current"
        self.backups = self.data / "backups"

    @contextlib.contextmanager
    def lock(self):
        self.data.mkdir(parents=True, exist_ok=True)
        with (self.data / ".operations.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def as_user(self, command, user="freedomshop", **kwargs):
        if os.geteuid() == 0:
            command = ["runuser", "--preserve-environment", "-u", user, "--", *command]
        return run(command, **kwargs)

    def environment(self, release):
        return dict(os.environ, DJANGO_SETTINGS_MODULE="settings_site",
                    PYTHONPATH=os.pathsep.join((str(self.config), str(release))),
                    PYTHONDONTWRITEBYTECODE="1")

    def manage(self, release, arguments):
        return self.as_user([str(release / ".venv/bin/python"), str(release / "manage.py"), *arguments],
                            cwd=release, env=self.environment(release), stdout=sys.stderr)

    def active(self):
        return subprocess.run(["systemctl", "is-active", "--quiet", "freedomshop.service"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def service(self, action):
        run(["systemctl", action, "freedomshop.service"], stdout=sys.stderr)

    @contextlib.contextmanager
    def paused(self):
        active = os.geteuid() == 0 and self.active()
        if active:
            self.service("stop")
        try:
            yield
        finally:
            if active:
                self.service("start")

    def backup(self, keep=14, prefix=""):
        """Four files; app.tar.gz also includes persistent media, uploads and secret."""
        if keep < 1:
            raise ValueError("--keep must be at least 1")
        release = self.current.resolve(strict=True)
        if not (release / "manage.py").is_file():
            raise ValueError("current is not an application release")
        self.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backups.chmod(0o700)
        folder = self.backups / (prefix + stamp())
        pending = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=self.backups))
        try:
            with self.paused():
                with tarfile.open(pending / "app.tar.gz", "w:gz") as archive:
                    def include(info):
                        parts = PurePosixPath(info.name).parts
                        if any(p in {".venv", ".git", "__pycache__", "staticfiles"} for p in parts):
                            return None
                        if info.issym() or info.islnk() or not (info.isfile() or info.isdir()):
                            raise ValueError(f"Unsupported file in backup: {info.name}")
                        return info
                    archive.add(release, arcname="app", filter=include)
                    for name in ("media", "uploads"):
                        if (self.data / name).exists():
                            archive.add(self.data / name, arcname=name, filter=include)
                    archive.add(self.config / "secret_key", arcname="config/secret_key", filter=include)
                with (pending / "database.dump").open("wb") as target:
                    self.as_user(["pg_dump", "-Fc", "--host=/var/run/postgresql", "--username=freedomshop",
                                  "--dbname=freedomshop"],
                                 stdout=target)
                if not (pending / "database.dump").stat().st_size:
                    raise ValueError("pg_dump produced an empty backup")
                shutil.copyfile(self.config / "settings_site.py", pending / "settings_site.py")
                for file in pending.iterdir():
                    file.chmod(0o600)
                (pending / "SHA256SUMS").write_text("".join(f"{checksum(pending / n)}  {n}\n" for n in BACKUP_FILES))
                verify_files(pending, BACKUP_FILES)
                pending.rename(folder)
            # Only timestamped routine backups are pruned. Safety backups are preserved.
            routine = sorted(p for p in self.backups.iterdir()
                             if STAMP.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
            for old in routine[:-keep]:
                shutil.rmtree(old)
            return folder
        finally:
            if pending.exists():
                shutil.rmtree(pending)

    def venv(self, release, wheelhouse=None):
        previous = os.umask(0o022)  # Root builds it, the application user must read/execute it.
        try:
            run(["python3", "-m", "venv", release / ".venv"], stdout=sys.stderr)
            command = [release / ".venv/bin/pip", "install", "-r", release / "requirements.lock"]
            if wheelhouse:
                command += ["--no-index", "--find-links", str(Path(wheelhouse).resolve())]
            run(command, stdout=sys.stderr)
            run([release / ".venv/bin/pip", "check"], stdout=sys.stderr)
        finally:
            os.umask(previous)

    def collectstatic(self, release):
        static = release / "staticfiles"
        static.mkdir(exist_ok=True)
        static.chmod(0o755)
        if os.geteuid() == 0:
            shutil.chown(static, user="freedomshop", group="freedomshop")
        self.manage(release, ["collectstatic", "--noinput"])
        if os.geteuid() == 0:
            for path in [static, *static.rglob("*")]:
                shutil.chown(path, user="root", group="root")
                path.chmod(0o755 if path.is_dir() else 0o644)

    def switch(self, release):
        link = self.root / ".current-new"
        link.unlink(missing_ok=True)
        link.symlink_to(release)
        link.replace(self.current)

    def install_cli(self, release):
        """Keep maintenance commands in step with an activated release."""
        destination = Path("/usr/local/lib/freedomshop")
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("operations.py", "release.py"):
            source = release / "deploy" / name
            # Versioned release archives always include these helpers.
            if not source.is_file():
                raise ValueError(f"Missing maintenance helper: {source}")
            pending = destination / f".{name}.new"
            shutil.copyfile(source, pending)
            pending.chmod(0o644)
            pending.replace(destination / name)
        wrapper = Path("/usr/local/bin/.freedomshop.new")
        shutil.copyfile(release / "deploy/freedomshop.sh", wrapper)
        wrapper.chmod(0o755)
        wrapper.replace(Path("/usr/local/bin/freedomshop"))

    def health(self, attempts=15):
        # Probe gunicorn directly; nginx owns TLS and overwrites this header publicly.
        request = urllib.request.Request("http://127.0.0.1:8001/health/",
                                         headers={"X-Forwarded-Proto": "https"})
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.load(response)
                    if response.status == 200 and payload.get("database") == "ok":
                        log("Health check passed: GET /health/")
                        return
            except (OSError, ValueError):
                pass
            if attempt + 1 < attempts:
                time.sleep(1)
        raise RuntimeError("GET /health/ failed")

    def unpack_backup(self, backup, target):
        verify_files(backup, BACKUP_FILES)
        extract(backup / "app.tar.gz", target, {"app", "media", "uploads", "config"})
        for name in ("app/manage.py", "app/requirements.lock", "config/secret_key"):
            if not (target / name).is_file():
                raise ValueError(f"Incomplete backup: {name}")

    def restore_database(self, backup):
        # A fresh database also removes objects introduced by failed migrations.
        self.as_user(["dropdb", "--if-exists", "--force", "--host=/var/run/postgresql", "--username=postgres",
                      "freedomshop"], user="postgres", stdout=sys.stderr)
        self.as_user(["createdb", "--owner=freedomshop", "--host=/var/run/postgresql", "--username=postgres",
                      "freedomshop"], user="postgres", stdout=sys.stderr)
        # Feed stdin: the service user cannot traverse root's private backup folder.
        with (backup / "database.dump").open("rb") as source:
            self.as_user(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner",
                          "--host=/var/run/postgresql", "--username=freedomshop", "--dbname=freedomshop"],
                         stdin=source, stdout=sys.stderr)

    def restore_files(self, backup, unpacked):
        shutil.copyfile(backup / "settings_site.py", self.config / "settings_site.py")
        (self.config / "settings_site.py").chmod(0o640)
        shutil.copyfile(unpacked / "config/secret_key", self.config / "secret_key")
        (self.config / "secret_key").chmod(0o600)
        if os.geteuid() == 0:
            shutil.chown(self.config / "settings_site.py", user="root", group="freedomshop")
            shutil.chown(self.config / "secret_key", user="freedomshop", group="freedomshop")
        for name in ("media", "uploads"):
            destination = self.data / name
            saved = self.data / f".{name}-restore-old"
            if saved.exists():
                raise RuntimeError(f"Interrupted restore needs inspection: {saved}")
            if destination.exists():
                destination.rename(saved)
            (unpacked / name).mkdir(exist_ok=True)
            shutil.move(str(unpacked / name), destination)
            if os.geteuid() == 0:
                for path in [destination, *destination.rglob("*")]:
                    shutil.chown(path, user="freedomshop", group="freedomshop")
            if saved.exists():
                shutil.rmtree(saved)

    def recover(self, backup, release):
        self.service("stop")
        with tempfile.TemporaryDirectory(prefix=".recover-", dir=self.backups) as folder:
            unpacked = Path(folder)
            self.unpack_backup(backup, unpacked)
            self.restore_database(backup)
            self.restore_files(backup, unpacked)
        self.switch(release)
        self.service("start")
        self.health()
        self.install_cli(release)

    def upgrade(self, archive, wheelhouse=None):
        require_root()
        archive = archive.resolve(strict=True)
        verify_files(archive.parent, [archive.name])
        old = self.current.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix=".release-", dir=self.releases) as folder:
            unpacked = Path(folder)
            # The archive must have exactly one conventional versioned top-level directory.
            with tarfile.open(archive, "r:gz") as source:
                roots = {PurePosixPath(m.name).parts[0] for m in source.getmembers() if PurePosixPath(m.name).parts}
            if len(roots) != 1 or not next(iter(roots)).startswith("freedomshop-"):
                raise ValueError("Expected a FreedomShop release archive")
            extract(archive, unpacked, roots)
            source = unpacked / next(iter(roots))
            version = version_from_text((source / "freedomshop/__version__.py").read_text())
            if source.name != f"freedomshop-{version}" or archive.name != f"freedomshop-{version}.tar.gz":
                raise ValueError("Archive name and release version disagree")
            release = self.releases / version
            if release.exists():
                raise ValueError(f"Release {version} already exists; use a new version")
            shutil.move(str(source), release)
        backup = None
        changed = False
        try:
            self.venv(release, wheelhouse)
            self.service("stop")
            backup = self.backup(prefix=f"before-{version}-")
            log(f"Rollback backup: {backup}")
            self.manage(release, ["migrate", "--plan"])
            changed = True  # Even a partially failed migration requires the database snapshot.
            self.manage(release, ["migrate", "--noinput"])
            self.manage(release, ["check"])
            self.collectstatic(release)
            self.switch(release)
            self.service("start")
            self.health()
            self.install_cli(release)
            log(f"Upgraded to {version}")
        except BaseException:
            log("Upgrade failed; restoring the previous release.")
            if changed and backup:
                self.recover(backup, old)
            else:
                self.switch(old)
                self.service("start")
            # Preserve failed code for diagnosis; never activate it on a re-run.
            raise

    def restore(self, backup, wheelhouse=None):
        require_root()
        backup = backup.resolve(strict=True)
        old = self.current.resolve(strict=True)
        self.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=self.backups) as folder:
            unpacked = Path(folder)
            self.unpack_backup(backup, unpacked)  # Validate before stopping the shop.
            release = self.releases / f"restored-{stamp()}"
            shutil.move(str(unpacked / "app"), release)
            self.venv(release, wheelhouse)
            self.service("stop")
            safety = None
            try:
                safety = self.backup(prefix="before-restore-")
                log(f"Pre-restore backup: {safety}")
                self.restore_database(backup)
                self.restore_files(backup, unpacked)
                self.manage(release, ["check"])
                self.collectstatic(release)
                self.switch(release)
                self.service("start")
                self.health()
                self.install_cli(release)
            except BaseException:
                if safety:
                    self.recover(safety, old)
                else:
                    self.service("start")
                raise


def main():
    parser = argparse.ArgumentParser(description="FreedomShop native installation maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Pause the app and back up code, files, settings and database")
    backup.add_argument("--keep", type=int, default=14, help="Keep this many routine backups (default 14)")
    for name, argument in (("upgrade", "archive"), ("restore", "backup")):
        child = commands.add_parser(name)
        child.add_argument(argument, type=Path)
        child.add_argument("--wheelhouse", type=Path, help="Install dependencies offline from this directory")
    commands.add_parser("status")
    commands.add_parser("shell", help="Django shell as the application user")
    manage = commands.add_parser("manage", help="Pass arguments to manage.py")
    manage.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    installation = Installation()
    os.umask(0o077)
    if args.command == "status":
        run(["systemctl", "--no-pager", "status", "freedomshop.service"])
        print(f"Current release: {installation.current.resolve()}")
        installation.health()
    elif args.command in {"manage", "shell"}:
        installation.manage(installation.current.resolve(strict=True),
                            ["shell"] if args.command == "shell" else args.arguments)
    else:
        with installation.lock():
            if args.command == "backup":
                print(installation.backup(keep=args.keep))
            elif args.command == "upgrade":
                installation.upgrade(args.archive, args.wheelhouse)
            elif args.command == "restore":
                installation.restore(args.backup, args.wheelhouse)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        log(f"ERROR: {error}")
        sys.exit(1)
