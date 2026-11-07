# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise file formats and failure recovery without changing the host install."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import mock, skipUnless

from django.test import SimpleTestCase

from . import build_deb, operations
from .install import hosts, render_settings
from .release import version_from_text

ROOT = Path(__file__).resolve().parent.parent


class PackagingTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="freedomshop-pkg-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.environment = dict(os.environ, FREEDOMSHOP_ROOT=str(self.base / "opt"),
                                FREEDOMSHOP_DATA_DIR=str(self.base / "data"),
                                FREEDOMSHOP_CONFIG_DIR=str(self.base / "etc"))
        self.environment_patch = mock.patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.site = operations.Installation()
        self.site.releases.mkdir(parents=True)
        self.site.config.mkdir()
        self.site.data.mkdir()
        self.old = self.site.releases / "0.1.0-dev"
        self.old.mkdir()
        (self.old / "manage.py").write_text("# demo app\n")
        (self.old / "requirements.lock").write_text("# demo dependencies\n")
        (self.old / "freedomshop").mkdir()
        (self.old / "freedomshop/__version__.py").write_text('__version__ = "0.1.0-dev"\n')
        self.site.current.symlink_to(self.old)
        (self.site.config / "settings_site.py").write_text("# demo settings\n")
        (self.site.config / "secret_key").write_text("only-a-test-secret")
        (self.site.data / "media").mkdir()
        (self.site.data / "media/logo.txt").write_text("demo logo")
        (self.site.data / "uploads").mkdir()
        (self.site.data / "uploads/note.txt").write_text("demo attachment")

    def command_stubs(self, fail=False):
        """CLI subprocesses cannot reach the host's service manager or database."""
        binary = self.base / "bin"
        binary.mkdir(exist_ok=True)
        scripts = {
            "pg_dump": '#!/usr/bin/env python3\nimport sys\n' +
                       ('sys.exit(9)\n' if fail else 'sys.stdout.buffer.write(b"PGDMP-demo")\n'),
            "systemctl": '#!/usr/bin/env python3\nimport sys\nsys.exit(3 if "is-active" in sys.argv else 0)\n',
            "runuser": '#!/usr/bin/env python3\nimport os, sys\na = sys.argv[sys.argv.index("--") + 1:]\nos.execvp(a[0], a)\n',
        }
        for name, script in scripts.items():
            path = binary / name
            path.write_text(script)
            path.chmod(0o755)
        return dict(self.environment, PATH=f"{binary}{os.pathsep}{os.environ['PATH']}")

    def snapshot(self, keep=14, fail=False):
        """The upgrade's rollback snapshot, using stubbed pg_dump/runuser/systemctl."""
        previous = os.umask(0o077)  # As main() sets it.
        try:
            with mock.patch.dict(os.environ, self.command_stubs(fail)):
                return self.site.backup(keep=keep)
        finally:
            os.umask(previous)

    def app_stub(self):
        """A release python that records the manage.py arguments and answers like the backup command."""
        venv = self.old / ".venv/bin"
        venv.mkdir(parents=True)
        calls = self.base / "manage-calls"
        (venv / "python").write_text("#!/usr/bin/env python3\nimport json, sys\n"
                                     f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[2:]) + '\\n')\n"
                                     "print(json.dumps({'stamp': '20261105-020000', 'status': 'verified'}))\n")
        (venv / "python").chmod(0o755)
        return calls

    def test_settings_template_renders_and_imports(self):
        rendered = render_settings((ROOT / "deploy/settings_site.template.py").read_text(),
                                   hosts("shop.example.com,192.0.2.10"), self.site.data,
                                   self.site.config / "secret_key")
        target = self.site.config / "settings_site.py"
        target.write_text(rendered)
        spec = importlib.util.spec_from_file_location("packaging_site_settings", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.SECRET_KEY, "only-a-test-secret")
        self.assertIn("shop.example.com", module.ALLOWED_HOSTS)
        self.assertEqual(module.DATABASES["default"]["HOST"], "/var/run/postgresql")
        self.assertTrue(module.PRINT_TESTING_MARKS)
        self.assertFalse(module.SECURE_SSL_REDIRECT)
        self.assertFalse(module.SESSION_COOKIE_SECURE)
        self.assertEqual(module.MEDIA_ROOT, self.site.data / "media")
        self.assertEqual(module.BACKUP_DIR, self.site.data / "backups")
        self.assertEqual(module.BACKUP_SETTINGS_FILES, [target.resolve(), self.site.config / "secret_key"])
        self.assertTrue(module.BACKUP_SCHEDULE_ENABLED_DEFAULT)
        self.assertNotIn("@@", rendered)

    def test_units_and_installer_let_the_app_back_up_and_restore(self):
        # The maintenance flag, backups and restore renames are under the data folder.
        service = (ROOT / "deploy/freedomshop.service").read_text()
        self.assertIn("ReadWritePaths=/var/lib/freedomshop\n", service)
        self.assertIn("ExecStart=/usr/local/bin/freedomshop manage run_scheduled_backup",
                      (ROOT / "deploy/freedomshop-backup.service").read_text())
        self.assertIn("OnCalendar=*:0/5", (ROOT / "deploy/freedomshop-backup.timer").read_text())
        installer = (ROOT / "deploy/install.py").read_text()
        self.assertIn('"--createdb"', installer)
        self.assertIn('"ALTER ROLE freedomshop CREATEDB"', installer)
        self.assertNotIn("--superuser\"", installer)

    def test_template_supports_https_and_safe_quoted_paths(self):
        rendered = render_settings((ROOT / "deploy/settings_site.template.py").read_text(),
                                   hosts("shop.example.com"), Path("/tmp/shop's data"),
                                   self.site.config / "secret_key", https=True)
        self.assertIn("SECURE_SSL_REDIRECT = True", rendered)
        compile(rendered, "settings_site.py", "exec")

    def test_host_input_rejects_configuration_injection(self):
        for value in ("https://shop.example.com", "shop.example.com;return", "*", "shop.example.com:8080", "a\"b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hosts(value)

    def test_upgrade_snapshot_produces_four_verified_files_and_persistent_data(self):
        folder = self.snapshot()
        self.assertEqual({p.name for p in folder.iterdir()}, {*operations.BACKUP_FILES, "SHA256SUMS"})
        operations.verify_files(folder, operations.BACKUP_FILES)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        for path in folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with tarfile.open(folder / "app.tar.gz") as archive:
            self.assertTrue({"app/manage.py", "media/logo.txt", "uploads/note.txt", "config/secret_key"}
                            <= set(archive.getnames()))
        restored = self.base / "unpacked"
        self.site.unpack_backup(folder, restored)
        self.assertEqual((restored / "config/secret_key").read_text(), "only-a-test-secret")

    def test_failed_dump_never_publishes_snapshot(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.snapshot(fail=True)
        self.assertEqual(list(self.site.backups.iterdir()), [])

    def test_snapshot_retention_preserves_app_backups_safety_and_unrelated_directories(self):
        for name in ("20260101T010101000001Z", "20260102T010101000001Z", "before-0.0.9-keep", "personal", "20261105-020000"):
            (self.site.backups / name).mkdir(parents=True)
        folder = self.snapshot(keep=1)
        self.assertEqual({p.name for p in self.site.backups.iterdir()},
                         {folder.name, "before-0.0.9-keep", "personal", "20261105-020000"})

    def test_corrupt_snapshot_is_rejected_before_recovery_touches_the_database(self):
        folder = self.snapshot()
        (folder / "database.dump").write_text("corrupted")
        with mock.patch.object(self.site, "service"), mock.patch.object(self.site, "restore_database") as database:
            with self.assertRaises(ValueError):
                self.site.recover(folder, self.old)
        database.assert_not_called()

    def test_cli_backup_runs_the_app_backup_command(self):
        calls = self.app_stub()
        result = subprocess.run(["bash", str(ROOT / "deploy/freedomshop.sh"), "backup", "--keep", "3"],
                                env=self.command_stubs(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.site.backups / "20261105-020000")
        self.assertEqual(json.loads(calls.read_text()), ["backup", "--keep", "3"])
        result = subprocess.run(["bash", str(ROOT / "deploy/freedomshop.sh"), "backup", "--keep", "0"],
                                env=self.command_stubs(), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def restore_mocks(self, stack):
        stack.enter_context(mock.patch.object(operations, "require_root"))
        mocks = {"chown": stack.enter_context(mock.patch.object(operations.shutil, "chown"))}
        for name in ("manage", "service", "health"):
            mocks[name] = stack.enter_context(mock.patch.object(self.site, name))
        stack.enter_context(mock.patch.object(self.site, "active", return_value=True))
        return mocks

    def test_cli_restore_copies_the_folder_in_then_restores_a_copy_then_switches(self):
        outside = self.base / "usb" / "20261105-020000"
        outside.mkdir(parents=True)
        (outside / "MANIFEST.json").write_text("{}")
        self.site.backups.mkdir()
        with ExitStack() as stack:
            mocks = self.restore_mocks(stack)
            self.site.app_restore(outside)
        self.assertTrue((self.site.backups / "20261105-020000/MANIFEST.json").is_file())
        mocks["chown"].assert_any_call(self.site.backups / "20261105-020000", user="freedomshop", group="freedomshop")
        self.assertEqual([call.args[1] for call in mocks["manage"].call_args_list],
                         [["restore_copy", "20261105-020000"], ["switch_over", "20261105-020000", "--yes"]])
        mocks["service"].assert_called_once_with("restart")
        mocks["health"].assert_called_once_with()

    def test_cli_restore_refuses_other_folders_and_stops_when_the_switch_fails(self):
        for folder in (self.base / "usb", self.base / "20261105-02000"):
            folder.mkdir(parents=True)
            with self.subTest(folder=folder.name), ExitStack() as stack:
                mocks = self.restore_mocks(stack)
                with self.assertRaises(ValueError):
                    self.site.app_restore(folder)
                mocks["manage"].assert_not_called()
        (self.site.backups / "20261105-020000").mkdir(parents=True)
        with ExitStack() as stack:
            mocks = self.restore_mocks(stack)
            mocks["manage"].side_effect = [None, subprocess.CalledProcessError(1, "switch_over")]
            with self.assertRaises(subprocess.CalledProcessError):
                self.site.app_restore(self.site.backups / "20261105-020000")
            mocks["service"].assert_not_called()
        mocks["chown"].assert_not_called()

    def test_archive_traversal_and_links_are_rejected(self):
        for name, link in (("app/../../escaped", False), ("/app/file", False), ("app/link", True)):
            with self.subTest(name=name):
                path = self.base / "bad.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    else:
                        member.size = 1
                    archive.addfile(member, None if link else io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    operations.extract(path, self.base / "target", {"app"})
                self.assertFalse((self.base / "escaped").exists())

    def release_archive(self):
        source = self.base / "freedomshop-0.2.0"
        (source / "freedomshop").mkdir(parents=True)
        (source / "freedomshop/__version__.py").write_text('__version__ = "0.2.0"\n')
        (source / "manage.py").write_text("# release app\n")
        (source / "requirements.lock").write_text("# dependencies\n")
        archive = self.base / "freedomshop-0.2.0.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            target.add(source, arcname=source.name)
        (self.base / "SHA256SUMS").write_text(f"{operations.checksum(archive)}  {archive.name}\n")
        return archive

    def test_upgrade_checksum_failure_never_stops_shop(self):
        archive = self.release_archive()
        archive.write_text("corrupted")
        with mock.patch.object(operations, "require_root"), mock.patch.object(self.site, "service") as service:
            with self.assertRaises(ValueError):
                self.site.upgrade(archive)
        service.assert_not_called()

    def upgrade_mocks(self, stack):
        stack.enter_context(mock.patch.object(operations, "require_root"))
        mocks = {}
        for name in ("venv", "service", "backup", "manage", "collectstatic", "health", "recover", "install_cli"):
            mocks[name] = stack.enter_context(mock.patch.object(self.site, name))
        mocks["backup"].return_value = self.base / "safety-backup"
        return mocks

    def test_upgrade_success_switches_only_after_checks(self):
        archive = self.release_archive()
        with ExitStack() as stack:
            mocks = self.upgrade_mocks(stack)
            def collect(release):
                self.assertEqual(self.site.current.resolve(), self.old)
            mocks["collectstatic"].side_effect = collect
            self.site.upgrade(archive)
            self.assertEqual(self.site.current.resolve(), self.site.releases / "0.2.0")
            self.assertEqual([call.args[1] for call in mocks["manage"].call_args_list],
                             [["migrate", "--plan"], ["migrate", "--noinput"], ["check"]])
            mocks["recover"].assert_not_called()

    def test_upgrade_migration_and_health_failures_restore_snapshot(self):
        for failure in ("migration", "health"):
            with self.subTest(failure=failure), ExitStack() as stack:
                archive = self.release_archive() if not (self.base / "freedomshop-0.2.0").exists() else self.base / "freedomshop-0.2.0.tar.gz"
                mocks = self.upgrade_mocks(stack)
                if failure == "migration":
                    mocks["manage"].side_effect = [None, RuntimeError("migration failed")]
                else:
                    mocks["health"].side_effect = RuntimeError("unhealthy release")
                with self.assertRaises(RuntimeError):
                    self.site.upgrade(archive)
                mocks["recover"].assert_called_once_with(self.base / "safety-backup", self.old)
                # Failed code is retained, so remove only this test's disposable release.
                import shutil
                shutil.rmtree(self.site.releases / "0.2.0")

    def test_backup_failure_prevents_migrations(self):
        archive = self.release_archive()
        with ExitStack() as stack:
            mocks = self.upgrade_mocks(stack)
            mocks["backup"].side_effect = RuntimeError("disk full")
            with self.assertRaises(RuntimeError):
                self.site.upgrade(archive)
            mocks["manage"].assert_not_called()
            self.assertEqual(self.site.current.resolve(), self.old)

    def test_restore_recreates_database_and_streams_private_dump(self):
        backup = self.base / "backup"
        backup.mkdir()
        (backup / "database.dump").write_bytes(b"PGDMP-demo")
        with mock.patch.object(self.site, "as_user") as command:
            self.site.restore_database(backup)
        self.assertEqual(command.call_args_list[0].args[0][:3], ["dropdb", "--if-exists", "--force"])
        self.assertEqual(command.call_args_list[1].args[0][:2], ["createdb", "--owner=freedomshop"])
        self.assertIn("--single-transaction", command.call_args_list[2].args[0])
        self.assertIn("stdin", command.call_args_list[2].kwargs)

    def test_installer_dry_run_is_unprivileged_and_has_no_writes(self):
        before = set(self.base.rglob("*"))
        result = subprocess.run([str(ROOT / "install.sh"), "--dry-run", "--allowed-hosts", "shop.example.com",
                                 "--https", "--wheelhouse", str(self.base / "wheels")],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-index --find-links", result.stdout)
        self.assertIn("certbot", result.stdout)
        self.assertEqual(before, set(self.base.rglob("*")))

    def test_version_parser_does_not_execute_code(self):
        self.assertEqual(version_from_text('__version__ = "0.1.0-dev"'), "0.1.0-dev")
        for source in ('__version__ = "../../escape"', '__version__ = str(1)', '__version__ = ""'):
            with self.assertRaises(ValueError):
                version_from_text(source)


@skipUnless(shutil.which("dpkg-deb"), "dpkg-deb is needed to build the Debian package")
class DebianPackageTests(SimpleTestCase):
    def test_package_carries_verified_release_and_runs_the_installer_steps(self):
        with tempfile.TemporaryDirectory(prefix="freedomshop-deb-", dir="/tmp") as folder:
            base = Path(folder)
            archive = base / "freedomshop-0.2.0.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for name, text in (("freedomshop/__version__.py", '__version__ = "0.2.0"\n'), ("LICENSE", "AGPL\n")):
                    data = text.encode()
                    member = tarfile.TarInfo(f"freedomshop-0.2.0/{name}")
                    member.size = len(data)
                    bundle.addfile(member, io.BytesIO(data))
            package = build_deb.build(archive, base / "out")
            self.assertEqual(package.name, "freedomshop_0.2.0_all.deb")
            contents = subprocess.check_output(["dpkg-deb", "--contents", str(package)], text=True)
            self.assertIn("./usr/share/freedomshop/freedomshop-0.2.0.tar.gz", contents)
            self.assertIn("./usr/share/freedomshop/SHA256SUMS", contents)
            control = subprocess.check_output(["dpkg-deb", "--field", str(package), "Version", "Depends"], text=True)
            self.assertIn("Version: 0.2.0", control)
            self.assertIn("postgresql", control)
            postinst = subprocess.check_output(["dpkg-deb", "--info", str(package), "postinst"], text=True)
            self.assertIn("install.py\" --no-apt", postinst)
            self.assertIn("freedomshop upgrade \"$ARCHIVE\"", postinst)
            self.assertEqual(subprocess.run(["sh", "-n"], input=postinst, text=True).returncode, 0)
        self.assertEqual(build_deb.debian_version("0.1.0-dev"), "0.1.0~dev")

    def test_installer_plan_can_skip_apt(self):
        result = subprocess.run([str(ROOT / "install.sh"), "--dry-run", "--no-apt", "--allowed-hosts", "shop.example.com"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skip apt-get", result.stdout)
        self.assertNotIn("apt-get install", result.stdout)
