# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from pathlib import Path
from unittest import mock, skipUnless

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.signin_helpers import AUTH_MIDDLEWARE, sign_in_admin, sign_in_register
from . import backups
from .middleware import BackupMaintenanceMiddleware
from .models import Backup, BackupSchedule, Business, RestoreLog


class BackupFolders:
    """Private temporary BACKUP_DIR, attachments and a settings file; one business row."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fs-backups-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.uploads = self.root / 'uploads'
        self.media.mkdir()
        self.uploads.mkdir()
        (self.media / 'attachment.txt').write_text('original attachment')
        (self.uploads / 'note.txt').write_text('original upload')
        self.site = self.root / 'settings_site.py'
        self.site.write_text('SECRET_KEY = "test-private-setting"\n')
        self.override = override_settings(BACKUP_DIR=self.root / 'backups', DATA_DIR=self.root,
                                         MEDIA_ROOT=self.media, BACKUP_UPLOAD_ROOT=self.uploads,
                                         TRACKER_UPLOAD_DIR=self.uploads, BACKUP_SETTINGS_FILES=[self.site])
        self.override.enable()
        self.addCleanup(self.override.disable)
        Business.objects.all().delete()
        Business.objects.create(name='Demo Furniture Co.', setup_complete=True)


@skipUnless(connection.vendor == 'sqlite', 'Copies the test database with the SQLite online backup API; '
            'the PostgreSQL path is covered by PostgresArgvTests and a scratch-cluster run.')
class BackupTests(BackupFolders, TransactionTestCase):
    """Actual SQLite backup API, archives, migration sanity and file-backed swaps."""
    def setUp(self):
        super().setUp()
        connection.ensure_connection()
        self.live_path = self.root / 'live.sqlite3'
        with sqlite3.connect(self.live_path) as target:
            connection.connection.backup(target)
        config = copy.deepcopy(connection.settings_dict)
        config['NAME'] = str(self.live_path)
        self.live = DatabaseWrapper(config, alias='backup_test_live')
        patch = mock.patch.object(backups, 'live_connection', return_value=self.live)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.live.close)

    def _name(self):
        with sqlite3.connect(self.live_path) as conn:
            return conn.execute('SELECT name FROM shop_settings_business').fetchone()[0]

    def _change_live(self):
        with sqlite3.connect(self.live_path) as conn:
            conn.execute('UPDATE shop_settings_business SET name = ?', ['Changed Shop'])
        (self.media / 'attachment.txt').write_text('changed attachment')
        self.site.write_text('SECRET_KEY = "test-changed-setting"\n')

    @override_settings(APP_VERSION='0.1.0-test')
    def test_create_verify_list_and_deterministic_download(self):
        row = backups.create_backup('manual')
        path = backups.checked_path(row.stamp)
        self.assertEqual({p.name for p in path.iterdir()}, set(backups.FILES))
        self.assertEqual(row.status, 'verified')
        manifest = backups.verify_backup(row)
        self.assertIn(['shop_settings', '0009_backup_backupschedule_restorelog'], manifest['schema'])
        self.assertEqual(manifest['database'], 'sqlite')
        self.assertEqual(manifest['app_version'], '0.1.0-test')
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertNotIn('test-private-setting', json.dumps(manifest))
        self.assertEqual(list(backups.list_backups()), [row])
        self.assertEqual(row.size, sum(p.stat().st_size for p in path.iterdir()))
        stream = io.BytesIO()
        digest = backups.write_download(row, stream)
        self.assertEqual(digest, row.download_sha256)
        self.assertEqual(digest, hashlib.sha256(stream.getvalue()).hexdigest())
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            self.assertEqual(set(archive.getnames()), {f'{row.stamp}/{name}' for name in backups.FILES})

    def test_corruption_marks_failed(self):
        row = backups.create_backup()
        (backups.checked_path(row.stamp) / 'media.tar.gz').write_bytes(b'broken')
        with self.assertRaisesRegex(backups.BackupError, 'SHA256 mismatch: media.tar.gz'):
            backups.verify_backup(row)
        row.refresh_from_db()
        self.assertEqual(row.status, 'failed')

    def test_backup_directory_handles_uri_punctuation_and_spaces(self):
        with override_settings(BACKUP_DIR=self.root / 'backups ?#é'):
            row = backups.create_backup()
            self.assertEqual(backups.verify_backup(row)['database'], 'sqlite')
            self.assertEqual(backups.restore_to_copy(row)['status'], 'ready')

    def test_configured_paths_may_sit_behind_a_link_but_nothing_below_them(self):
        # A data folder reached through a link (a mounted disk) must not break backups or every request.
        disk = self.root / 'disk'
        disk.mkdir()
        (self.root / 'linked').symlink_to(disk, target_is_directory=True)
        with override_settings(BACKUP_DIR=self.root / 'linked' / 'backups', MEDIA_ROOT=self.root / 'linked' / 'media'):
            (disk / 'media').mkdir()
            (disk / 'media' / 'logo.txt').write_text('logo')
            row = backups.create_backup()
            self.assertEqual(row.status, 'verified')
            self.assertEqual(backups.backup_root(), disk.resolve() / 'backups')
            self.assertEqual(backups.restore_to_copy(row)['status'], 'ready')
            (disk / 'backups' / '20260101-010101').symlink_to(self.media, target_is_directory=True)
            with self.assertRaises(backups.BackupError):
                backups.checked_path('20260101-010101')

    @override_settings(DATA_DIR=None)
    def test_backup_dir_follows_data_dir_set_by_site_settings(self):
        from django.conf import settings
        del settings.BACKUP_DIR
        settings.DATA_DIR = self.root / 'site-data'
        self.assertEqual(backups.backup_root(), (self.root / 'site-data' / 'backups').resolve())

    def test_non_regular_upload_refused(self):
        (self.media / 'link').symlink_to(self.site)
        with self.assertRaisesRegex(backups.BackupError, 'symbolic link'):
            backups.create_backup()
        self.assertEqual(Backup.objects.get().status, 'failed')

    def test_path_traversal_symlinks_and_forged_model_path_rejected(self):
        for stamp in ('../', '..', '/tmp', '20261105-250000', '20261105-000000/../../'):
            with self.subTest(stamp=stamp), self.assertRaises(backups.BackupError):
                backups.checked_path(stamp)
        row = backups.create_backup()
        row.path = str(self.root)
        with self.assertRaisesRegex(backups.BackupError, 'path or file set'):
            backups.verify_backup(row)
        symlink = backups.backup_root() / '20260101-010101'
        symlink.symlink_to(self.media, target_is_directory=True)
        with self.assertRaises(backups.BackupError):
            backups.checked_path(symlink.name)

    def test_unsafe_tar_rejected_even_with_recomputed_checksums(self):
        row = backups.create_backup()
        path = backups.checked_path(row.stamp)
        with tarfile.open(path / 'media.tar.gz', 'w:gz') as archive:
            info = tarfile.TarInfo('media/../../outside')
            info.size = 4
            archive.addfile(info, io.BytesIO(b'evil'))
        manifest = json.loads((path / 'MANIFEST.json').read_text())
        manifest['sizes']['media.tar.gz'] = (path / 'media.tar.gz').stat().st_size
        backups._json(path / 'MANIFEST.json', manifest)
        with (path / 'SHA256SUMS').open('w') as stream:
            for name in backups.PAYLOAD:
                stream.write(f'{backups.sha256(path / name)}  {name}\n')
        with self.assertRaisesRegex(backups.BackupError, 'Unsafe'):
            backups.verify_backup(row)
        self.assertFalse((self.root / 'outside').exists())

    def test_restore_copy_checks_database_and_preserves_live(self):
        row = backups.create_backup()
        self._change_live()
        report = backups.restore_to_copy(row)
        self.assertEqual(report['status'], 'ready', report)
        self.assertEqual(report['business_rows'], 1)
        self.assertTrue(report['migrations_current'])
        self.assertGreater(report['table_count'], 10)
        self.assertEqual(self._name(), 'Changed Shop')
        self.assertEqual((self.media / 'attachment.txt').read_text(), 'changed attachment')
        again = backups.restore_to_copy(row)
        self.assertEqual(again['status'], 'failed')
        self.assertIn('already exists', again['error'])

    def test_missing_business_fails_copy_sanity(self):
        with sqlite3.connect(self.live_path) as conn:
            conn.execute('DELETE FROM shop_settings_business')
        row = backups.create_backup()
        report = backups.restore_to_copy(row)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['step'], 'check database copy')
        self.assertIn('business row', report['error'])

    def test_copy_tampering_prevents_switch_and_safety(self):
        row = backups.create_backup()
        self.assertEqual(backups.restore_to_copy(row)['status'], 'ready')
        (backups._stage_path(row.stamp) / 'media' / 'media' / 'attachment.txt').write_text('tampered')
        with self.assertRaisesRegex(backups.BackupError, 'changed after verification'):
            backups.switch_over(row, confirmed=True)
        self.assertFalse(Backup.objects.filter(reason='safety').exists())
        self.assertEqual(self._name(), 'Demo Furniture Co.')

    def test_confirmation_is_required(self):
        row = backups.create_backup()
        with self.assertRaisesRegex(backups.BackupError, 'confirmation'):
            backups.switch_over(row)
        self.assertFalse(Backup.objects.filter(reason='safety').exists())

    def test_failed_safety_never_swaps(self):
        row = backups.create_backup()
        backups.restore_to_copy(row)
        self._change_live()
        with mock.patch.object(backups, '_create_backup', side_effect=backups.BackupError('safety unavailable')):
            with self.assertRaisesRegex(backups.BackupError, 'safety backup'):
                backups.switch_over(row, confirmed=True)
        self.assertEqual(self._name(), 'Changed Shop')
        self.assertEqual(self.site.read_text(), 'SECRET_KEY = "test-changed-setting"\n')
        self.assertFalse(backups.control_path('MAINTENANCE').exists())
        self.assertEqual(RestoreLog.objects.filter(action='switch').get().step, 'safety backup')

    def test_preexisting_safety_backup_cannot_authorize_switch(self):
        old_safety = backups.create_backup('safety')
        row = backups.create_backup()
        backups.restore_to_copy(row)
        self._change_live()
        with mock.patch.object(backups, '_create_backup', return_value=old_safety):
            with self.assertRaisesRegex(backups.BackupError, 'fresh verified safety backup'):
                backups.switch_over(row, confirmed=True)
        self.assertEqual(self._name(), 'Changed Shop')
        self.assertFalse(backups.control_path('MAINTENANCE').exists())

    def test_failure_after_safety_leaves_live_untouched(self):
        row = backups.create_backup()
        backups.restore_to_copy(row)
        self._change_live()
        before = self.live_path.read_bytes()
        with mock.patch.object(backups, '_checkpoint', side_effect=backups.BackupError('injected')):
            with self.assertRaisesRegex(backups.BackupError, 'after safety backup'):
                backups.switch_over(row, confirmed=True)
        self.assertEqual(self.live_path.read_bytes(), before)
        self.assertEqual(self._name(), 'Changed Shop')
        self.assertEqual(Backup.objects.filter(reason='safety', status='verified').count(), 1)
        self.assertFalse(backups.control_path('MAINTENANCE').exists())

    def test_successful_switch_restores_files_and_records_safety(self):
        row = backups.create_backup()
        backups.restore_to_copy(row)
        self._change_live()
        report = backups.switch_over(row, confirmed=True)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(self._name(), 'Demo Furniture Co.')
        self.assertEqual((self.media / 'attachment.txt').read_text(), 'original attachment')
        self.assertEqual((self.uploads / 'note.txt').read_text(), 'original upload')
        self.assertEqual(self.site.read_text(), 'SECRET_KEY = "test-private-setting"\n')
        self.assertTrue(Backup.objects.filter(stamp=report['safety_stamp'], reason='safety', status='verified').exists())
        self.assertEqual(RestoreLog.objects.filter(action='switch').get().status, 'complete')
        self.assertFalse(backups.control_path('MAINTENANCE').exists())

    def test_switch_leaves_unchanged_settings_alone_and_keeps_unwritable_ones(self):
        # Native installs keep site settings in a folder the app cannot write.
        row = backups.create_backup()
        backups.restore_to_copy(row)
        report = backups.switch_over(row, confirmed=True)
        self.assertEqual(report['status'], 'complete')
        self.assertNotIn('settings_kept', report)
        self.assertFalse(list(self.root.glob('.settings_site.py.*')))
        row = backups.create_backup()
        backups.restore_to_copy(row)
        self._change_live()
        writable = backups.os.access
        with mock.patch.object(backups.os, 'access', side_effect=lambda p, m: False if Path(p) == self.root else writable(p, m)):
            report = backups.switch_over(row, confirmed=True)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['settings_kept'], ['settings_site.py'])
        self.assertEqual(self.site.read_text(), 'SECRET_KEY = "test-changed-setting"\n')
        self.assertEqual(self._name(), 'Demo Furniture Co.')
        self.assertEqual((self.media / 'attachment.txt').read_text(), 'original attachment')

    def test_real_default_database_catalog_survives_switch(self):
        original = connections['default']
        original_alias = self.live.alias
        self.live.alias = 'default'
        connections['default'] = self.live
        try:
            schedule = BackupSchedule.objects.create(enabled=True, time=time(4), retention=7)
            row = backups.create_backup()
            created_at = row.created_at
            backups.restore_to_copy(row)
            self._change_live()
            report = backups.switch_over(row, confirmed=True)
            self.assertEqual(Business.objects.get().name, 'Demo Furniture Co.')
            self.assertEqual(Backup.objects.get(stamp=row.stamp).created_at, created_at)
            self.assertEqual(Backup.objects.get(stamp=row.stamp).restore_report['status'], 'complete')
            self.assertEqual(Backup.objects.get(stamp=report['safety_stamp']).reason, 'safety')
            self.assertEqual(RestoreLog.objects.filter(action='switch').get().status, 'complete')
            self.assertEqual(BackupSchedule.objects.get().retention, schedule.retention)
        finally:
            self.live.close()
            connections['default'] = original
            self.live.alias = original_alias

    def test_failure_after_database_and_files_rolls_back(self):
        for checkpoint in ('after_database', 'after_files'):
            with self.subTest(checkpoint=checkpoint):
                row = backups.create_backup()
                backups.restore_to_copy(row)
                self._change_live()
                def fail(step):
                    if step == checkpoint:
                        raise backups.BackupError('injected')
                with mock.patch.object(backups, '_checkpoint', side_effect=fail):
                    with self.assertRaisesRegex(backups.BackupError, 'Live data is unchanged'):
                        backups.switch_over(row, confirmed=True)
                self.assertEqual(self._name(), 'Changed Shop')
                self.assertEqual((self.media / 'attachment.txt').read_text(), 'changed attachment')
                self.assertEqual(self.site.read_text(), 'SECRET_KEY = "test-changed-setting"\n')
                row.refresh_from_db()
                self.assertTrue(row.restore_report['rolled_back'])

    def test_rollback_failure_keeps_maintenance(self):
        row = backups.create_backup()
        backups.restore_to_copy(row)
        def fail(step):
            if step == 'after_database':
                raise backups.BackupError('injected')
        with mock.patch.object(backups, '_checkpoint', side_effect=fail), mock.patch.object(
                backups, '_rollback_database', side_effect=backups.BackupError('rollback unavailable')):
            with self.assertRaisesRegex(backups.BackupError, 'operator recovery'):
                backups.switch_over(row, confirmed=True)
        self.assertTrue(backups.control_path('MAINTENANCE').exists())
        self.assertEqual(json.loads((backups._stage_path(row.stamp) / 'SWITCH.json').read_text())['status'], 'recovery_required')

    def test_uncertain_database_promotion_keeps_maintenance(self):
        row = backups.create_backup()
        backups.restore_to_copy(row)
        with mock.patch.object(backups, '_swap_database', side_effect=backups.RecoveryRequired('promotion uncertain')):
            with self.assertRaisesRegex(backups.BackupError, 'operator recovery'):
                backups.switch_over(row, confirmed=True)
        self.assertTrue(backups.control_path('MAINTENANCE').exists())

    def test_sqlite_promotion_rename_failure_restores_original(self):
        row = backups.create_backup()
        backups.restore_to_copy(row)
        self._change_live()
        rename = Path.rename
        def fail(source, target):
            if source.name.startswith('.live.sqlite3.restore-') and target == self.live_path:
                raise OSError('injected promotion failure')
            return rename(source, target)
        with mock.patch.object(Path, 'rename', autospec=True, side_effect=fail):
            with self.assertRaisesRegex(backups.BackupError, 'database swap'):
                backups.switch_over(row, confirmed=True)
        self.assertEqual(self._name(), 'Changed Shop')
        self.assertFalse(backups.control_path('MAINTENANCE').exists())
        self.assertFalse(list(self.root.glob('.live.sqlite3.restore-*')))

    def test_retention_only_after_verified_schedule_keeps_manual_safety(self):
        manual = backups.create_backup('manual')
        safety = backups.create_backup('safety')
        scheduled = [backups.create_backup('scheduled') for _ in range(3)]
        BackupSchedule.objects.create(enabled=True, time=time(2), retention=2)
        now = timezone.make_aware(datetime(2026, 11, 5, 3))
        newest = backups.run_scheduled_backup(now)
        self.assertIsNotNone(newest)
        self.assertTrue(Backup.objects.filter(stamp=manual.stamp).exists())
        self.assertTrue(Backup.objects.filter(stamp=safety.stamp).exists())
        self.assertEqual(Backup.objects.filter(reason='scheduled').count(), 2)
        self.assertFalse(backups.checked_path(scheduled[0].stamp).exists())
        self.assertIsNone(backups.run_scheduled_backup(now))
        self.assertEqual(Backup.objects.filter(reason='scheduled').count(), 2)

    def test_retention_counts_only_verified_scheduled_backups(self):
        good = backups.create_backup('scheduled')
        failed = []
        for _ in range(2):
            row = backups.create_backup('scheduled')
            Backup.objects.filter(stamp=row.stamp).update(status='failed')
            failed.append(row)
        BackupSchedule.objects.create(enabled=True, time=time(2), retention=2)
        newest = backups.run_scheduled_backup(timezone.make_aware(datetime(2026, 11, 5, 3)))
        # Two newer failed nights must not push the older verified backup out of a retention of 2.
        self.assertTrue(backups.checked_path(good.stamp).exists())
        self.assertEqual(Backup.objects.filter(reason='scheduled').count(), 4)
        backups.prune_scheduled(1)
        self.assertEqual(list(Backup.objects.filter(reason='scheduled').values_list('stamp', flat=True)), [newest.stamp])
        self.assertFalse(backups.checked_path(good.stamp).exists())

    def test_backup_command_keep_prunes_scheduled_only_after_verifying(self):
        manual = backups.create_backup('manual')
        old = [backups.create_backup('scheduled') for _ in range(2)]
        output = io.StringIO()
        call_command('backup', reason='scheduled', keep=1, stdout=output)
        newest = json.loads(output.getvalue())['stamp']
        self.assertEqual(list(Backup.objects.filter(reason='scheduled').values_list('stamp', flat=True)), [newest])
        self.assertTrue(Backup.objects.filter(stamp=manual.stamp).exists())
        self.assertFalse(any(backups.checked_path(r.stamp).exists() for r in old))
        with self.assertRaises(CommandError):
            call_command('backup', keep=0, stdout=io.StringIO())

    def test_schedule_starts_on_only_where_site_settings_say_a_timer_runs(self):
        self.assertFalse(backups.get_schedule().enabled)
        BackupSchedule.objects.all().delete()
        with override_settings(BACKUP_SCHEDULE_ENABLED_DEFAULT=True):
            self.assertTrue(backups.get_schedule().enabled)
            BackupSchedule.objects.update(enabled=False)
            self.assertFalse(backups.get_schedule().enabled)

    def test_failed_schedule_does_not_prune_or_repeat_same_day(self):
        old = backups.create_backup('scheduled')
        BackupSchedule.objects.create(enabled=True, time=time(2), retention=1)
        now = timezone.make_aware(datetime(2026, 11, 5, 3))
        with mock.patch.object(backups, '_dump', side_effect=backups.BackupError('dump unavailable')):
            with self.assertRaises(backups.BackupError):
                backups.run_scheduled_backup(now)
        self.assertTrue(backups.checked_path(old.stamp).exists())
        self.assertIsNone(backups.run_scheduled_backup(now))
        self.assertIn('dump unavailable', BackupSchedule.objects.get().last_error)

    def test_schedule_disabled_before_time_and_next_run(self):
        schedule = BackupSchedule.objects.create(time=time(2))
        now = timezone.make_aware(datetime(2026, 11, 5, 1))
        self.assertIsNone(backups.run_scheduled_backup(now))
        self.assertIsNone(backups.next_run(schedule, now))
        schedule.enabled = True
        schedule.save()
        self.assertIsNone(backups.run_scheduled_backup(now))
        self.assertEqual(backups.next_run(schedule, now).hour, 2)
        schedule.last_attempt_day = now.date()
        self.assertEqual(backups.next_run(schedule, now).date(), now.date() + timedelta(days=1))

    def test_scheduler_uses_saved_shop_zone_without_wsgi_startup(self):
        Business.objects.update(time_zone='America/Los_Angeles')
        schedule = BackupSchedule.objects.create(enabled=True, time=time(2))
        before = datetime(2026, 11, 5, 9, 30, tzinfo=datetime_timezone.utc)
        due = datetime(2026, 11, 5, 10, 5, tzinfo=datetime_timezone.utc)
        self.assertIsNone(backups.run_scheduled_backup(before))
        self.assertEqual(str(backups.next_run(schedule, before).tzinfo), 'America/Los_Angeles')
        self.assertIsNotNone(backups.run_scheduled_backup(due))
        self.assertIsNone(backups.run_scheduled_backup(due))

    @override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
    def test_page_download_and_restore_admin_only_with_typed_confirmation(self):
        row = backups.create_backup()
        sign_in_register(self.client)
        base = f'/settings/backups/{row.stamp}'
        self.assertEqual(self.client.get('/settings/backups/').status_code, 302)
        self.assertEqual(self.client.get(f'{base}/download/').status_code, 302)
        self.assertEqual(self.client.post(f'{base}/copy/').status_code, 403)
        self.assertEqual(self.client.post(f'{base}/switch/', {'confirmation': 'RESTORE'}).status_code, 403)
        self.assertEqual(self.client.post(f'{base}/discard/').status_code, 403)
        sign_in_admin(self.client)
        response = self.client.get('/settings/backups/')
        self.assertContains(response, 'Back up now')
        self.assertContains(response, row.download_sha256)
        self.assertNotContains(response, 'test-private-setting')
        response = self.client.get(f'{base}/download/')
        payload = b''.join(response.streaming_content)
        self.assertEqual(response['X-Checksum-SHA256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/settings/backups/../download/').status_code, 404)
        self.assertEqual(self.client.get('/settings/backups/%2E%2E/download/').status_code, 404)
        self.assertEqual(self.client.get(f'{base}/copy/').status_code, 405)
        self.assertEqual(self.client.post(f'{base}/copy/').status_code, 302)
        self.assertContains(self.client.get(f'{base}/report/'), 'Type RESTORE')
        with mock.patch.object(backups, 'switch_over') as switch:
            self.assertEqual(self.client.post(f'{base}/switch/', {'confirmation': 'restore'}).status_code, 400)
            switch.assert_not_called()
        self.assertContains(self.client.get(f'{base}/report/'), 'Discard restore copy')
        self.assertEqual(self.client.get(f'{base}/discard/').status_code, 405)
        self.assertEqual(self.client.post(f'{base}/discard/').status_code, 302)
        self.assertContains(self.client.get('/settings/backups/'), 'Restore into a copy')
        self.assertEqual(self.client.post('/settings/backups/', {'action': 'schedule', 'enabled': 'on', 'time': '03:30', 'retention': '0'}).status_code, 400)

    def test_maintenance_503_precedes_database_and_auth(self):
        backups.control_path('MAINTENANCE').write_text('restore')
        target = mock.Mock(return_value=HttpResponse('live'))
        middleware = BackupMaintenanceMiddleware(target)
        with self.assertNumQueries(0), mock.patch.object(backups, 'live_connection', side_effect=AssertionError('must not query')):
            response = middleware(RequestFactory().get('/sales/'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Retry-After'], '60')
        target.assert_not_called()

    def test_operation_lock_refuses_concurrent_backup(self):
        with backups.file_lock():
            with self.assertRaisesRegex(backups.BackupError, 'Another backup or restore'):
                backups.create_backup()
        self.assertFalse(Backup.objects.exists())

    def test_maintenance_refuses_core_and_cli_before_database_reads(self):
        row = backups.create_backup()
        backups.control_path('MAINTENANCE').write_text('recovery')
        for action in (backups.create_backup, backups.run_scheduled_backup, backups.list_backups,
                       lambda: backups.restore_to_copy(row)):
            with self.subTest(action=action), self.assertNumQueries(0), self.assertRaisesRegex(backups.BackupError, 'Maintenance'):
                action()
        for command, args in [('list_backups', []), ('restore_copy', [row.stamp]), ('switch_over', [row.stamp])]:
            with self.subTest(command=command), self.assertNumQueries(0), self.assertRaisesRegex(CommandError, 'Maintenance'):
                call_command(command, *args, stdout=io.StringIO())

    @override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
    def test_recovery_response_never_refreshes_unknown_live_database(self):
        row = backups.create_backup()
        sign_in_admin(self.client)
        def fail(*args, **kwargs):
            backups.control_path('MAINTENANCE').write_text('recovery')
            raise backups.RecoveryRequired('Stopped at database swap; operator recovery required.')
        with mock.patch.object(backups, 'switch_over', side_effect=fail), mock.patch.object(Backup, 'refresh_from_db') as refresh:
            response = self.client.post(f'/settings/backups/{row.stamp}/switch/', {'confirmation': 'RESTORE'})
            self.assertContains(response, 'Stopped at database swap', status_code=503)
            refresh.assert_not_called()

    def test_pending_migration_refuses_copy(self):
        row = backups.create_backup()
        with mock.patch.object(backups.MigrationExecutor, 'migration_plan', return_value=[object()]):
            report = backups.restore_to_copy(row)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('needs migrations', report['error'])

    @override_settings(MIDDLEWARE=AUTH_MIDDLEWARE)
    def test_csrf_is_required_for_mutations(self):
        from django.test import Client
        with override_settings(MIDDLEWARE=[*AUTH_MIDDLEWARE, 'django.middleware.csrf.CsrfViewMiddleware']):
            client = Client(enforce_csrf_checks=True)
            sign_in_admin(client)
            with mock.patch.object(backups, 'create_backup') as create:
                self.assertEqual(client.post('/settings/backups/', {'action': 'create'}).status_code, 403)
                create.assert_not_called()
            row = backups.create_backup()
            for action, name in (('copy', 'restore_to_copy'), ('switch', 'switch_over'), ('discard', 'discard_copy')):
                with self.subTest(action=action), mock.patch.object(backups, name) as called:
                    self.assertEqual(client.post(f'/settings/backups/{row.stamp}/{action}/', {'confirmation': 'RESTORE'}).status_code, 403)
                    called.assert_not_called()

    def test_commands(self):
        output = io.StringIO()
        call_command('backup', reason='manual', stdout=output)
        stamp = json.loads(output.getvalue())['stamp']
        output = io.StringIO()
        call_command('list_backups', stdout=output)
        self.assertEqual(json.loads(output.getvalue())[0]['stamp'], stamp)
        call_command('restore_copy', stamp, stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('switch_over', stamp, stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('restore_copy', '../', stdout=io.StringIO())

    def test_failed_copy_can_be_discarded_and_restored_again(self):
        row = backups.create_backup()
        with mock.patch.object(backups, '_sanity_database', side_effect=backups.BackupError('injected')):
            self.assertEqual(backups.restore_to_copy(row)['status'], 'failed')
        self.assertEqual(backups.restore_to_copy(row)['error'],
                         'A restore copy already exists; review its report before switching.')
        call_command('restore_copy', row.stamp, discard=True, stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.restore_report, {})
        self.assertFalse((backups.control_path('.restore') / row.stamp).exists())
        self.assertEqual(backups.restore_to_copy(row)['status'], 'ready')
        self.assertTrue(RestoreLog.objects.filter(backup=row, action='discard').exists())
        row.restore_report = {'status': 'recovery_required'}
        row.save()
        with self.assertRaisesRegex(backups.BackupError, 'operator recovery'):
            backups.discard_copy(row)
        self.assertTrue((backups.control_path('.restore') / row.stamp).exists())

    def test_restore_copy_adopts_a_backup_folder_without_a_catalog_row(self):
        # A fresh install, or a folder copied from another disk, has no Backup row yet.
        row = backups.create_backup('manual')
        Backup.objects.all().delete()
        output = io.StringIO()
        call_command('restore_copy', row.stamp, stdout=output)
        self.assertEqual(json.loads(output.getvalue())['status'], 'ready')
        adopted = Backup.objects.get(stamp=row.stamp)
        self.assertEqual((adopted.reason, adopted.status), ('manual', 'verified'))
        with self.assertRaisesRegex(CommandError, 'No backup'):
            call_command('restore_copy', '20200101-000000', stdout=io.StringIO())
        damaged = backups.create_backup('manual')
        Backup.objects.filter(stamp=damaged.stamp).delete()
        (backups.checked_path(damaged.stamp) / 'db.dump').write_bytes(b'damaged')
        with self.assertRaisesRegex(CommandError, 'SHA256 mismatch'):
            call_command('restore_copy', damaged.stamp, stdout=io.StringIO())
        self.assertEqual(Backup.objects.get(stamp=damaged.stamp).status, 'failed')


class PostgresArgvTests(BackupFolders, TransactionTestCase):
    """PostgreSQL subprocess and rename calls, mocked; runs on either test database."""
    def test_postgres_dump_verify_restore_use_explicit_argv(self):
        config = copy.deepcopy(connection.settings_dict)
        config.update(ENGINE='django.db.backends.postgresql', NAME='demo_shop', USER='demo_role', HOST='/var/run/postgresql', PORT='5432', PASSWORD='private')
        fake = mock.Mock(settings_dict=config)
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[0] == 'pg_dump':
                Path(argv[argv.index('--file') + 1]).write_bytes(b'PGDMP')
            return mock.Mock(stdout=b'; archive list')
        with mock.patch.object(backups, 'live_connection', return_value=fake), mock.patch.object(
                backups, '_schema', return_value=[]), mock.patch.object(backups.subprocess, 'run', side_effect=run), mock.patch.object(
                backups, '_sanity_database', return_value={'table_count': 40, 'business_rows': 1, 'migrations_current': True}):
            row = backups.create_backup()
            report = backups.restore_to_copy(row)
            backups.discard_copy(row)
        self.assertEqual(report['status'], 'ready', report)
        common = ['--host', '/var/run/postgresql', '--port', '5432', '--username', 'demo_role']
        path = backups.checked_path(row.stamp)
        self.assertIn(['pg_dump', '-Fc', *common, '--file', str(path / 'db.dump'), '--dbname', 'demo_shop'], [a for a, _ in calls])
        self.assertIn(['pg_restore', '--list', str(path / 'db.dump')], [a for a, _ in calls])
        self.assertIn(['createdb', *common, '--template=template0', '--', f'demo_shop_restore_{row.stamp}'], [a for a, _ in calls])
        self.assertIn(['dropdb', *common, '--if-exists', '--', f'demo_shop_restore_{row.stamp}'], [a for a, _ in calls])
        self.assertIn(['pg_restore', *common, '--exit-on-error', '--no-owner', '--no-privileges', '--dbname', f'demo_shop_restore_{row.stamp}', str(path / 'db.dump')], [a for a, _ in calls])
        for argv, kwargs in calls:
            self.assertIsInstance(argv, list)
            self.assertNotIn('shell', kwargs)
            self.assertEqual(kwargs['env']['PGPASSWORD'], 'private')
            self.assertNotIn('private', argv)

    def test_postgres_renames_are_one_transaction_and_identifiers_quoted(self):
        admin = mock.MagicMock()
        with mock.patch.object(backups, '_pg_admin', return_value=admin):
            backups._pg_rename({'NAME': 'demo'}, 'demo', 'demo_restore', 'demo_before')
        sql_calls = admin.cursor.return_value.__enter__.return_value.execute.call_args_list
        self.assertEqual(len(sql_calls), 5)
        self.assertIn('pg_terminate_backend', sql_calls[0].args[0])
        self.assertIn('Identifier', repr(sql_calls[1].args[0]))
        admin.commit.assert_called_once_with()
        admin.close.assert_called_once_with()

    def test_postgres_refused_rename_is_a_plain_failure_but_lost_commit_needs_recovery(self):
        import psycopg
        admin = mock.MagicMock()
        admin.cursor.return_value.__enter__.return_value.execute.side_effect = [None, psycopg.errors.ObjectInUse('in use')]
        with mock.patch.object(backups, '_pg_admin', return_value=admin):
            with self.assertRaises(backups.BackupError) as caught:
                backups._pg_rename({'NAME': 'demo'}, 'demo', 'demo_restore', 'demo_before')
        self.assertNotIsInstance(caught.exception, backups.RecoveryRequired)
        admin.commit.assert_not_called()
        admin = mock.MagicMock()
        admin.commit.side_effect = psycopg.OperationalError('server closed the connection')
        with mock.patch.object(backups, '_pg_admin', return_value=admin):
            with self.assertRaises(backups.RecoveryRequired):
                backups._pg_rename({'NAME': 'demo'}, 'demo', 'demo_restore', 'demo_before')
