# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server Status: "RAID & drives" (root-written hw.json + live /proc/mdstat), certificate fallback, and sample-server backups.

Fixtures in accounts/fixtures/hwstatus/ contain made-up host and hardware identities. Cases that must
stay fresh are written to temp files with "generated" relative to now, so they never turn stale."""
import copy
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from . import server_status as ss

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'hwstatus'
SAMPLE_HW = FIXTURES / 'sample-hw.json'
SAMPLE_MDSTAT = FIXTURES / 'sample-mdstat.txt'
SAMPLE_NOW = datetime(2026, 10, 16, 9, 25, tzinfo=dt_timezone.utc)
MDSTAT_DEGRADED = """Personalities : [raid1] [raid0] [raid6] [raid5] [raid4] [raid10]
md127 : active raid1 sdb2[0]
      498925568 blocks super 1.2 [2/1] [U_]
      bitmap: 1/4 pages [4KB], 65536KB chunk

unused devices: <none>
"""
MDSTAT_REBUILD = """Personalities : [raid1]
md127 : active raid1 sda2[2] sdb2[0]
      498925568 blocks super 1.2 [2/1] [U_]
      [========>............]  recovery = 42.3% (211234567/498925568) finish=20.1min speed=238000K/sec
      bitmap: 1/4 pages [4KB], 65536KB chunk

unused devices: <none>
"""
MDSTAT_NONE = 'Personalities : \nunused devices: <none>\n'


def sample_hw():
    return json.loads(SAMPLE_HW.read_text())


def degraded_hw():
    hw = sample_hw()
    a = hw['raid'][0]
    a.update(state='clean, degraded', active_devices=1, failed_devices=1, degraded=True,
             members=[{'device': '/dev/sdb2', 'state': 'active sync'}, {'device': '/dev/sda2', 'state': 'faulty'}])
    hw['event'] = 'Fail /dev/md127 /dev/sda2'
    return hw


class HwCase(TestCase):
    """Each test gets its own temp folder for hw.json and mdstat; settings/paths are pointed at them."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fs-hwstatus-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hw_path = self.tmp / 'hw.json'
        self.md_path = self.tmp / 'mdstat'
        self.md_path.write_text(MDSTAT_NONE)
        settings_patch = override_settings(HW_STATUS_FILE=str(self.hw_path))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        md_patch = mock.patch.object(ss, 'MDSTAT_PATH', self.md_path)
        md_patch.start()
        self.addCleanup(md_patch.stop)

    def write(self, hw, fresh=True):
        hw = copy.deepcopy(hw)
        if fresh:
            hw['generated'] = (timezone.now() - timedelta(minutes=3)).isoformat()
        self.hw_path.write_text(json.dumps(hw))

    def mdstat(self, text):
        self.md_path.write_text(text)

    def raid(self):
        return ss.raid_section().items

    def by_label(self, items):
        return {i.label: i for i in items}


class RaidSectionTests(HwCase):
    def test_sample_server_file_is_all_ok(self):
        shutil.copy(SAMPLE_HW, self.hw_path)
        shutil.copy(SAMPLE_MDSTAT, self.md_path)
        with mock.patch('django.utils.timezone.now', return_value=SAMPLE_NOW):
            items = self.raid()
        rows = self.by_label(items)
        self.assertEqual(list(rows), ['Array /dev/md127', 'Drive /dev/sda', 'Drive /dev/sdb'])
        self.assertEqual({i.state for i in items}, {'ok'})
        md = rows['Array /dev/md127']
        self.assertEqual(md.value, 'OK · 2 of 2 drives working')
        self.assertIn('live /proc/mdstat: [2/2] [UU]', md.detail)
        self.assertIn('/dev/sda2 active sync', md.detail)
        self.assertIn('last mdadm event: TestMessage /dev/md/root', md.detail)   # event only in the detail
        self.assertEqual(rows['Drive /dev/sda'].value, 'PASSED · 40 °C')
        self.assertIn('ExampleDisk Model 500', rows['Drive /dev/sda'].detail)
        self.assertIn('79 power-on hours', rows['Drive /dev/sda'].detail)

    def test_sample_file_stale_after_30_minutes_keeps_rows(self):
        shutil.copy(SAMPLE_HW, self.hw_path)
        with mock.patch('django.utils.timezone.now', return_value=SAMPLE_NOW + timedelta(minutes=45)):
            items = self.raid()
        self.assertEqual(items[0].state, 'warn')
        self.assertEqual(items[0].value, 'RAID/drive status not updated since 10/16/2026 5:20 AM')
        self.assertIn('configured status collector', items[0].detail)
        self.assertEqual([i.label for i in items[1:]], ['Array /dev/md127', 'Drive /dev/sda', 'Drive /dev/sdb'])

    def test_fresh_relative_file_is_not_stale(self):
        self.write(sample_hw())
        self.assertEqual({i.state for i in self.raid()}, {'ok'})

    def test_missing_file_and_no_arrays_is_one_muted_row(self):
        items = self.raid()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].value, items[0].state), ('not set up on this host', 'muted'))

    def test_missing_file_with_live_arrays_uses_mdstat(self):
        shutil.copy(SAMPLE_MDSTAT, self.md_path)
        items = self.raid()
        self.assertEqual([(i.label, i.state) for i in items], [('Drive details', 'muted'), ('Array /dev/md127', 'ok')])
        self.assertIn('[2/2] [UU]', items[1].detail)
        self.mdstat(MDSTAT_DEGRADED)
        items = self.raid()
        self.assertEqual((items[1].value, items[1].state), ('DEGRADED (live /proc/mdstat)', 'bad'))

    def test_bad_json_warns(self):
        self.hw_path.write_text('{"generated": ')
        items = self.raid()
        self.assertEqual([(i.value, i.state) for i in items], [('not valid JSON', 'warn')])

    def test_degraded(self):
        self.write(degraded_hw())
        md = self.by_label(self.raid())['Array /dev/md127']
        self.assertEqual(md.state, 'bad')
        self.assertEqual(md.value, 'DEGRADED: 1 of 2 drives working — replace the failed drive')
        self.assertIn('/dev/sda2 faulty', md.detail)
        self.assertIn('Fail /dev/md127 /dev/sda2', md.detail)

    def test_rebuilding_while_degraded(self):
        hw = degraded_hw()
        hw['raid'][0].update(sync_action='recover', sync_progress_pct=42.3, state='clean, degraded, recovering')
        self.write(hw)
        md = self.by_label(self.raid())['Array /dev/md127']
        self.assertEqual((md.value, md.state), ('REBUILDING 1 of 2 drives, 42% — do not power off', 'bad'))

    def test_scheduled_check_is_ok_with_percent(self):
        hw = sample_hw()
        hw['raid'][0].update(sync_action='check', sync_progress_pct=37.0, state='active, checking')
        self.write(hw)
        md = self.by_label(self.raid())['Array /dev/md127']
        self.assertEqual((md.value, md.state), ('OK · scheduled check 37%', 'ok'))

    def test_mismatch_after_check_warns(self):
        hw = sample_hw()
        hw['raid'][0]['mismatch_cnt'] = 128
        self.write(hw)
        md = self.by_label(self.raid())['Array /dev/md127']
        self.assertEqual(md.state, 'warn')
        self.assertIn('128 mismatched', md.value)

    def test_live_mdstat_degraded_overrides_clean_file(self):
        self.write(sample_hw())
        self.mdstat(MDSTAT_DEGRADED)
        md = self.by_label(self.raid())['Array /dev/md127']
        self.assertEqual((md.value, md.state), ('DEGRADED (live /proc/mdstat)', 'bad'))
        self.assertIn('[2/1] [U_]', md.detail)
        self.mdstat(MDSTAT_REBUILD)
        md = self.by_label(self.raid())['Array /dev/md127']
        self.assertEqual(md.state, 'bad')
        self.assertIn('REBUILDING, 42%', md.value)

    def test_live_mdstat_degraded_even_when_file_is_stale(self):
        shutil.copy(SAMPLE_HW, self.hw_path)
        self.mdstat(MDSTAT_DEGRADED)
        with mock.patch('django.utils.timezone.now', return_value=SAMPLE_NOW + timedelta(hours=2)):
            states = [i.state for i in self.raid()]
        self.assertEqual(states[0], 'warn')
        self.assertIn('bad', states)

    def test_smart_failed_hot_and_sectors(self):
        hw = sample_hw()
        hw['disks'][0].update(smart='FAILED')
        hw['disks'][1].update(temperature_c=65)
        hw['disks'].append(dict(hw['disks'][1], device='/dev/sdc', temperature_c=38, pending_sectors=3))
        hw['disks'].append(dict(hw['disks'][1], device='/dev/sdd', temperature_c=38, reallocated_sectors=8))
        hw['disks'].append(dict(hw['disks'][1], device='/dev/sde', temperature_c=None, smart=''))
        self.write(hw)
        rows = self.by_label(self.raid())
        self.assertEqual((rows['Drive /dev/sda'].value, rows['Drive /dev/sda'].state), ('FAILED — replace this drive · 40 °C', 'bad'))
        self.assertEqual(rows['Drive /dev/sdb'].state, 'warn')
        self.assertIn('65 °C · running hot', rows['Drive /dev/sdb'].value)
        self.assertEqual(rows['Drive /dev/sdc'].state, 'warn')
        self.assertIn('3 pending sectors', rows['Drive /dev/sdc'].value)
        self.assertEqual(rows['Drive /dev/sdd'].state, 'warn')
        self.assertIn('8 reallocated sectors', rows['Drive /dev/sdd'].value)
        self.assertEqual((rows['Drive /dev/sde'].value, rows['Drive /dev/sde'].state), ('unknown', 'warn'))

    def test_parse_mdstat(self):
        arrays = ss.parse_mdstat(SAMPLE_MDSTAT.read_text())
        self.assertEqual(arrays, [{'name': 'md127', 'state': 'active', 'level': 'raid1', 'counts': '[2/2]', 'status': '[UU]',
                                   'degraded': False, 'action': '', 'pct': None}])
        rebuild = ss.parse_mdstat(MDSTAT_REBUILD)[0]
        self.assertEqual((rebuild['degraded'], rebuild['action'], rebuild['pct']), (True, 'recovery', 42.3))
        self.assertEqual(ss.parse_mdstat(MDSTAT_NONE), [])


class HealthDotTests(HwCase):
    def test_degraded_array_turns_page_and_dot_red(self):
        from django.core.cache import cache
        self.write(degraded_hw())
        self.assertEqual(ss.overall(ss.gather())[0], 'bad')
        cache.delete(ss.CACHE_KEY)
        self.addCleanup(cache.delete, ss.CACHE_KEY)
        request = RequestFactory().get('/')
        with mock.patch('accounts.admin_mode.is_on', return_value=True):
            self.assertEqual(ss.health(request), {'server_health': 'bad'})


class CertificateTests(HwCase):
    def test_openssl_unreadable_falls_back_to_hw_json(self):
        hw = sample_hw()
        self.write(hw)
        with mock.patch.object(ss, 'run', return_value=(1, '', 'Could not open file')), \
                mock.patch('django.utils.timezone.now', return_value=SAMPLE_NOW):
            item = ss.cert_item(ss.CERT_PATHS[0])
        self.assertEqual((item.value, item.state), ('expires 1/13/2027 (89 days)', 'ok'))
        self.assertIn('server status file', item.detail)
        self.assertNotIn('setfacl', item.detail)
        with mock.patch.object(ss, 'run', return_value=(1, '', 'nope')), \
                mock.patch('django.utils.timezone.now', return_value=datetime(2027, 1, 1, tzinfo=dt_timezone.utc)):
            self.assertEqual(ss.cert_item(ss.CERT_PATHS[0]).state, 'warn')
        hw['cert'] = {'path': 'x', 'not_after': None, 'days_left': 5}
        self.write(hw)
        with mock.patch.object(ss, 'run', return_value=(1, '', 'nope')):
            item = ss.cert_item(ss.CERT_PATHS[0])
        self.assertEqual((item.value, item.state), ('expires in 5 days', 'bad'))

    def test_no_file_no_cert(self):
        with mock.patch.object(ss, 'run', return_value=(1, '', 'nope')), mock.patch.object(ss, '_exists', return_value=True):
            item = ss.cert_item(ss.CERT_PATHS[0])
        self.assertEqual((item.value, item.state, item.detail), ('not readable by the app user', 'muted', str(ss.CERT_PATHS[0])))
        with mock.patch.object(ss, 'run', return_value=(1, '', 'nope')), mock.patch.object(ss, '_exists', return_value=False):
            self.assertEqual(ss.cert_item(ss.CERT_PATHS[0]).value, 'not on this host (development)')


def fake_systemctl(units):
    """run stand-in: units = {'freedomshop-hourly-backup': (timer_props, service_props)}; missing units are 'not-found'; None = systemctl fails."""
    def run(cmd, timeout=5):
        if units is None:
            return None, '', 'systemctl: not found'
        unit, kind = cmd[2].rsplit('.', 1)
        props = units.get(unit)
        if props is None:
            return 0, 'LoadState=not-found\nActiveState=inactive', ''
        return 0, '\n'.join(f'{k}={v}' for k, v in props[0 if kind == 'timer' else 1].items()), ''
    return run


HOURLY_OK = ({'LoadState': 'loaded', 'ActiveState': 'active', 'LastTriggerUSec': 'Fri 2026-10-16 05:00:24 EDT',
              'NextElapseUSecRealtime': 'Fri 2026-10-16 06:00:00 EDT'}, {'Result': 'success', 'ExecMainStatus': '0'})


class BackupsTests(HwCase):
    def test_sample_server_no_nightly_timer(self):
        with mock.patch.object(ss, 'run', fake_systemctl({'freedomshop-hourly-backup': HOURLY_OK})), \
                mock.patch.object(ss.platform, 'node', return_value='sample-server'), \
                mock.patch.object(ss, 'NIGHTLY_ROOT', self.tmp / 'nope'):
            s = ss.backups_section()
        self.assertEqual([(i.label, i.value, i.state) for i in s.items], [
            ('Nightly local snapshot', 'No local nightly snapshot on this server (hourly off-site backups only)', 'muted'),
            ('Hourly encrypted Borg backup', 'scheduled · last run succeeded', 'ok')])
        self.assertNotIn('Nightly snapshots live on this server', s.note)
        self.assertIn('no local nightly snapshot', s.note)
        self.assertIn('configured encrypted repositories', s.note)

    def test_sample_secondary_keeps_both_timers_and_wording(self):
        nightly = ({'LoadState': 'loaded', 'ActiveState': 'active', 'LastTriggerUSec': 'x', 'NextElapseUSecRealtime': 'y'},
                   {'Result': 'success', 'ExecMainStatus': '0'})
        snaps = self.tmp / 'nightly'
        (snaps / 'nightly-20261016').mkdir(parents=True)
        with mock.patch.object(ss, 'run', fake_systemctl({'freedomshop-hourly-backup': HOURLY_OK, 'freedomshop-backup': nightly})), \
                mock.patch.object(ss.platform, 'node', return_value='sample-secondary'), mock.patch.object(ss, 'NIGHTLY_ROOT', snaps):
            s = ss.backups_section()
        self.assertEqual([i.label for i in s.items], [ss.NIGHTLY_LABEL, 'Newest nightly snapshot', ss.HOURLY_LABEL])
        self.assertEqual([i.state for i in s.items], ['ok', 'ok', 'ok'])
        self.assertIn('Nightly snapshots live on this server', s.note)

    @override_settings(BACKUP_HOURLY_LABEL='Hourly backup (custom)')
    def test_label_setting_overrides(self):
        self.assertEqual(ss.hourly_label(), 'Hourly backup (custom)')

    def test_hourly_backup_from_hw_json_when_systemctl_fails(self):
        hw = sample_hw()
        self.write(hw)
        with mock.patch.object(ss, 'run', fake_systemctl(None)), mock.patch.object(ss.platform, 'node', return_value='sample-server'), \
                mock.patch.object(ss, 'NIGHTLY_ROOT', self.tmp / 'nope'):
            s = ss.backups_section()
        self.assertEqual(len(s.items), 2)
        hourly = s.items[1]
        self.assertEqual((hourly.value, hourly.state), ('scheduled · last run succeeded', 'ok'))
        self.assertIn('Fri 2026-10-16 05:00:24 EDT', hourly.detail)
        hw['backup']['freedomshop-hourly-backup']['Result'] = 'exit-code'
        hw['backup']['freedomshop-hourly-backup']['ExecMainStatus'] = '2'
        self.write(hw)
        with mock.patch.object(ss, 'run', fake_systemctl(None)), mock.patch.object(ss, 'NIGHTLY_ROOT', self.tmp / 'nope'):
            hourly = ss.backups_section().items[1]
        self.assertEqual((hourly.value, hourly.state), ('last run failed (exit-code, exit 2)', 'bad'))
        hw['backup']['timer_active'] = 'inactive'
        hw['backup']['freedomshop-hourly-backup']['Result'] = 'success'
        self.write(hw)
        with mock.patch.object(ss, 'run', fake_systemctl(None)), mock.patch.object(ss, 'NIGHTLY_ROOT', self.tmp / 'nope'):
            self.assertEqual(ss.backups_section().items[1].state, 'bad')

    def test_sample_server_root_only_nightly_folder_reads_hw_json(self):
        # The nightly timer exists but has not fired yet; the snapshot was made by hand.
        # When the snapshot folder is root-only, the status file supplies its row.
        nightly = ({'LoadState': 'loaded', 'ActiveState': 'active', 'LastTriggerUSec': '',
                    'NextElapseUSecRealtime': 'Sat 2026-10-17 02:30:00 EDT'},
                   {'Result': 'success', 'ExecMainStatus': '0', 'ExecMainExitTimestamp': 'Fri 2026-10-16 05:25:48 EDT'})
        hw = sample_hw()
        made = timezone.now() - timedelta(hours=2)
        hw['backup']['nightly_snapshots'] = {'count': 1, 'newest': {'name': 'nightly-20261016-052546',
                                                                     'mtime': made.isoformat(), 'bytes': 11260243}}
        self.write(hw)
        with mock.patch.object(ss, 'run', fake_systemctl({'freedomshop-hourly-backup': HOURLY_OK, 'freedomshop-backup': nightly})), \
                mock.patch.object(ss.platform, 'node', return_value='sample-server'), \
                mock.patch.object(ss.Path, 'iterdir', side_effect=PermissionError):
            s = ss.backups_section()
        self.assertEqual([(i.label, i.state) for i in s.items], [
            (ss.NIGHTLY_LABEL, 'ok'), ('Newest nightly snapshot', 'ok'), (ss.HOURLY_LABEL, 'ok')])
        self.assertIn('run by hand Fri 2026-10-16 05:25:48 EDT', s.items[0].detail)
        self.assertTrue(s.items[1].value.startswith('nightly-20261016-052546'))
        self.assertIn('from the server status file', s.items[1].detail)
        self.assertEqual(ss.overall([s])[0], 'ok')
        # An old snapshot still warns, and a hw.json without the nightly block falls back to the muted row.
        hw['backup']['nightly_snapshots']['newest']['mtime'] = (timezone.now() - timedelta(hours=40)).isoformat()
        self.write(hw)
        with mock.patch.object(ss.Path, 'iterdir', side_effect=PermissionError):
            self.assertEqual(ss._nightly_snapshot_item().state, 'warn')
        del hw['backup']['nightly_snapshots']
        self.write(hw)
        with mock.patch.object(ss.Path, 'iterdir', side_effect=PermissionError):
            self.assertEqual(ss._nightly_snapshot_item().state, 'muted')

    def test_storage_has_no_smart_rows(self):
        items = ss.storage_section().items
        self.assertFalse([i for i in items if 'Drive' in i.label or 'SMART' in i.label or 'sudo' in i.detail])


UPSC_OL = 'battery.charge: 100\nbattery.runtime: 2700\ndevice.type: ups\ninput.voltage: 119.0\nups.load: 9\nups.model: Tripp Lite UPS \nups.status: OL\n'


def fake_upsc(output, code=0):
    def run(cmd, timeout=5):
        assert cmd[:2] == ['upsc', ss.UPS_NAME], cmd
        return code, output, ''
    return run


class PowerSectionTests(TestCase):
    """Server Status 'Power (UPS)' from NUT's upsc, no sudo."""

    def section(self, output, code=0, which='/usr/bin/upsc'):
        with mock.patch.object(ss, 'run', fake_upsc(output, code)), mock.patch.object(ss.shutil, 'which', return_value=which):
            return ss.power_section()

    def test_on_line_power_is_ok(self):
        s = self.section(UPSC_OL)
        self.assertEqual([(i.label, i.state) for i in s.items], [('Battery backup', 'ok'), ('Battery', 'ok'), ('Load', 'ok')])
        self.assertEqual(s.items[0].value, 'On line power')
        self.assertIn('Tripp Lite UPS · input 119 V', s.items[0].detail)
        self.assertEqual(s.items[1].value, '100% · about 45 min of runtime')
        self.assertEqual(s.items[2].value, '9% of the UPS capacity')
        self.assertEqual(ss.overall([s])[0], 'ok')

    def test_on_battery_warns_and_low_battery_is_bad(self):
        s = self.section(UPSC_OL.replace('ups.status: OL', 'ups.status: OB DISCHRG').replace('battery.charge: 100', 'battery.charge: 45'))
        self.assertEqual((s.items[0].state, s.items[0].value), ('warn', 'On battery — power is out · discharging'))
        self.assertEqual(s.items[1].state, 'warn')
        self.assertEqual(ss.overall([s])[0], 'warn')
        s = self.section(UPSC_OL.replace('ups.status: OL', 'ups.status: OB LB').replace('battery.charge: 100', 'battery.charge: 12').replace('battery.runtime: 2700', 'battery.runtime: 90'))
        self.assertEqual(s.items[0].state, 'bad')
        self.assertIn('LOW BATTERY', s.items[0].value)
        self.assertEqual((s.items[1].state, s.items[1].value), ('bad', '12% · about 90 s of runtime'))
        self.assertEqual(ss.overall([s])[0], 'bad')

    def test_no_reading_is_bad(self):
        for output, code in [('', 1), ('Error: Driver not connected', 1), ('', 0)]:
            s = self.section(output, code)
            self.assertEqual([(i.label, i.state, i.value) for i in s.items], [('Battery backup', 'bad', 'UPS not reporting')], output)
        s = self.section(UPSC_OL, 0, which=None)  # NUT not installed (dev machines, previews): muted, not red
        self.assertEqual([(i.label, i.state, i.value) for i in s.items], [('Battery backup', 'muted', 'no UPS software on this machine')])
        self.assertEqual(ss.overall([s])[0], 'ok')
        s = self.section(UPSC_OL.replace('ups.status: OL\n', 'ups.status: \n'))
        self.assertEqual(s.items[0].state, 'bad')

    def test_power_section_is_gathered_first(self):
        self.assertEqual(ss.SECTION_BUILDERS[0], ('Power (UPS)', 'power_section'))
