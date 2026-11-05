# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server Status (Admin Mode): read-only facts about the host, software, storage, database and backups.

Everything here is gathered with unprivileged reads and short subprocess calls that a Django worker under
ProtectSystem=strict can make. Facts that need root (RAID detail, SMART drive health, certificate expiry and backup
results) come from a root-written status file (settings.HW_STATUS_FILE); the RAID state is
also cross-checked live against the world-readable /proc/mdstat. The app never uses sudo (the service runs with
NoNewPrivileges=true). Anything still unreadable is shown grey with the reason. Nothing on this page changes the server.
"""
import json
import os
import re
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from importlib import metadata
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

PACKAGES = ['Django', 'gunicorn', 'psycopg', 'Pillow', 'openpyxl', 'reportlab', 'pypdfium2']
DATA_DIR = Path(getattr(settings, 'DATA_DIR', '/var/lib/freedomshop'))
NIGHTLY_ROOT = Path(getattr(settings, 'BACKUP_NIGHTLY_ROOT', DATA_DIR / 'backups' / 'nightly'))
NIGHTLY_UNIT = getattr(settings, 'BACKUP_NIGHTLY_UNIT', 'freedomshop-backup')
NIGHTLY_LABEL = getattr(settings, 'BACKUP_NIGHTLY_LABEL', 'Nightly app + database backup')
HOURLY_UNIT = getattr(settings, 'BACKUP_HOURLY_UNIT', 'freedomshop-hourly-backup')
HOURLY_LABEL = 'Hourly encrypted Borg backup'
APP_UNIT = getattr(settings, 'SERVER_APP_UNIT', 'freedomshop')
APP_DIR = Path(getattr(settings, 'SERVER_APP_DIR', Path(__file__).resolve().parent.parent))
DATABASE_DIR = Path(getattr(settings, 'SERVER_DATABASE_DIR', '/var/lib/postgresql'))
CERT_PATHS = [Path(p) for p in getattr(settings, 'SERVER_CERT_PATHS', [])]
MDSTAT_PATH = Path('/proc/mdstat')
HW_STATUS_DEFAULT = str(DATA_DIR / 'hw.json')
HW_STALE_MINUTES = 30
DISK_HOT_C = 60


@dataclass
class Item:
    label: str
    value: str
    state: str = 'ok'      # ok | warn | bad | muted
    detail: str = ''


@dataclass
class Section:
    title: str
    items: list = field(default_factory=list)
    note: str = ''


def run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, '', str(exc)


def _exists(path):
    """Path.exists that treats 'permission denied' as 'exists but not readable' rather than raising."""
    try:
        return path.exists()
    except PermissionError:
        return True
    except OSError:
        return False


def human_bytes(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:,.1f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024


def ago(dt):
    delta = timezone.now() - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return 'in the future'
    if seconds < 90:
        return f'{seconds} s ago'
    if seconds < 5400:
        return f'{seconds // 60} min ago'
    if seconds < 172800:
        return f'{seconds / 3600:.1f} h ago'
    return f'{seconds // 86400} days ago'


# ---- Sections ----------------------------------------------------------------------------------------------------

def system_section():
    s = Section('System')
    pretty = ''
    try:
        for line in Path('/etc/os-release').read_text().splitlines():
            if line.startswith('PRETTY_NAME='):
                pretty = line.split('=', 1)[1].strip().strip('"')
    except OSError:
        pass
    s.items.append(Item('Operating system', pretty or platform.platform()))
    s.items.append(Item('Kernel', platform.release()))
    s.items.append(Item('Hostname', platform.node()))
    try:
        up = float(Path('/proc/uptime').read_text().split()[0])
        s.items.append(Item('Uptime', str(timedelta(seconds=int(up)))))
    except (OSError, ValueError):
        s.items.append(Item('Uptime', 'not readable', 'muted'))
    try:
        load = os.getloadavg()
        cpus = os.cpu_count() or 1
        state = 'ok' if load[0] < cpus else ('warn' if load[0] < cpus * 2 else 'bad')
        s.items.append(Item('Load average (1 / 5 / 15 min)', f'{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}', state, f'{cpus} CPU{"s" if cpus != 1 else ""}'))
    except OSError:
        pass
    s.items.append(Item('Server time', timezone.localtime().strftime('%A, %B %-d, %Y %-I:%M:%S %p %Z')))
    code, out, _ = run(['timedatectl', 'show', '--property=NTPSynchronized', '--value'])
    if code == 0 and out:
        s.items.append(Item('Clock synchronized (NTP)', 'Yes' if out == 'yes' else 'No', 'ok' if out == 'yes' else 'warn'))
    return s


def memory_section():
    s = Section('Memory')
    try:
        info = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, rest = line.partition(':')
            info[key] = int(rest.split()[0]) * 1024
        total, avail = info['MemTotal'], info['MemAvailable']
        used_pct = (total - avail) / total * 100
        state = 'ok' if used_pct < 80 else ('warn' if used_pct < 92 else 'bad')
        s.items.append(Item('RAM', f'{human_bytes(total - avail)} used of {human_bytes(total)} ({used_pct:.0f}%)', state, f'{human_bytes(avail)} available'))
        swap_total, swap_free = info.get('SwapTotal', 0), info.get('SwapFree', 0)
        if swap_total:
            swap_pct = (swap_total - swap_free) / swap_total * 100
            s.items.append(Item('Swap', f'{human_bytes(swap_total - swap_free)} used of {human_bytes(swap_total)} ({swap_pct:.0f}%)', 'ok' if swap_pct < 50 else 'warn'))
        else:
            s.items.append(Item('Swap', 'none configured', 'muted'))
    except (OSError, KeyError, ValueError) as exc:
        s.items.append(Item('RAM', f'not readable ({exc})', 'muted'))
    return s


def storage_section():
    s = Section('Storage')
    seen = set()
    for label, path in [('System (/)', '/'), ('Application', str(APP_DIR)), ('Shop data', str(DATA_DIR)),
                        ('PostgreSQL data', str(DATABASE_DIR)), ('Backups', str(NIGHTLY_ROOT))]:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        key = (st.f_fsid if hasattr(st, 'f_fsid') else None, st.f_blocks)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = (total - free) / total * 100 if total else 0
        state = 'ok' if used_pct < 80 else ('warn' if used_pct < 90 else 'bad')
        detail = 'same filesystem as above' if key in seen else ''
        seen.add(key)
        s.items.append(Item(label, f'{human_bytes(free)} free of {human_bytes(total)} ({used_pct:.0f}% used)', state, detail))
    # Drive health (SMART) and RAID live in the "RAID & drives" section.
    return s


# ---- Hardware status file + /proc/mdstat ------------------------------------------------------------------------

def hw_status_path():
    return Path(getattr(settings, 'HW_STATUS_FILE', HW_STATUS_DEFAULT) or HW_STATUS_DEFAULT)


def read_hw():
    """(data, problem): data is the parsed hw.json dict or None; problem is '' | 'missing' | a short reason."""
    path = hw_status_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, 'missing'
    except OSError as exc:
        return None, f'not readable ({exc.strerror or exc.__class__.__name__})'
    try:
        data = json.loads(text)
    except ValueError:
        return None, 'not valid JSON'
    if not isinstance(data, dict):
        return None, 'not valid JSON'
    return data, ''


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def _local(dt):
    return timezone.localtime(dt).strftime('%-m/%-d/%Y %-I:%M %p')


MD_HEADER = re.compile(r'^(md\S*)\s*:\s*(.*)$')
MD_STATUS = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')
MD_PROGRESS = re.compile(r'\b(resync|recovery|check|repair|reshape)\s*=\s*([\d.]+)%')


def parse_mdstat(text):
    """Arrays in /proc/mdstat: [{name, state, level, counts '[2/2]', status '[UU]', degraded, action, pct}]."""
    arrays = []
    for line in (text or '').splitlines():
        m = MD_HEADER.match(line)
        if m:
            words = m.group(2).split()
            words = [w for w in words if not w.startswith('(')]       # drop "(auto-read-only)" etc.
            level = next((w for w in words[1:] if w.startswith('raid') or w in ('linear', 'multipath')), '')
            arrays.append({'name': m.group(1), 'state': words[0] if words else '', 'level': level,
                           'counts': '', 'status': '', 'degraded': False, 'action': '', 'pct': None})
            continue
        if not arrays or not line.startswith((' ', '\t')):
            continue
        st = MD_STATUS.search(line)
        if st:
            arrays[-1].update(counts=f'[{st.group(1)}/{st.group(2)}]', status=f'[{st.group(3)}]',
                              degraded='_' in st.group(3) or st.group(1) != st.group(2))
        pr = MD_PROGRESS.search(line)
        if pr:
            arrays[-1].update(action=pr.group(1), pct=float(pr.group(2)))
    return arrays


def read_mdstat():
    try:
        return parse_mdstat(MDSTAT_PATH.read_text())
    except OSError:
        return []


def _level_name(level):
    level = str(level or '')
    return f'RAID {level[4:]}' if level.startswith('raid') else (level or 'array')


def _pct(value):
    try:
        return f'{float(value):.0f}%'
    except (TypeError, ValueError):
        return ''


def _array_item(a):
    device = a.get('device') or '?'
    raid_n, active_n, failed_n = a.get('raid_devices'), a.get('active_devices'), a.get('failed_devices') or 0
    state_words = [w.strip().lower() for w in str(a.get('state') or '').split(',') if w.strip()]
    action = str(a.get('sync_action') or 'idle').lower()
    pct = _pct(a.get('sync_progress_pct'))
    degraded = bool(a.get('degraded')) or failed_n > 0 or (raid_n is not None and active_n is not None and active_n < raid_n) \
        or 'degraded' in state_words
    healthy_state = bool(state_words) and state_words[0] in ('clean', 'active') and not {'failed', 'inactive'} & set(state_words)
    members = ', '.join(f"{m.get('device', '?')} {m.get('state', '')}".strip() for m in a.get('members') or [])
    size = str(a.get('size') or '')
    size = size[size.find('(') + 1:].split()[0:2] if '(' in size else []
    detail = ' · '.join(x for x in [_level_name(a.get('level')), ' '.join(size), f'members: {members}' if members else ''] if x)
    working = f'{active_n} of {raid_n} drives'
    if degraded:
        if action in ('resync', 'recover', 'recovery') :
            value = f'REBUILDING {working}{", " + pct if pct else ""} — do not power off'
        else:
            value = f'DEGRADED: {working} working — replace the failed drive'
        state = 'bad'
    elif not healthy_state:
        value, state = f"array state: {a.get('state') or 'unknown'}", 'bad'
    elif action in ('check', 'repair'):
        value, state = f'OK · scheduled {action}{" " + pct if pct else ""}', 'ok'
    elif action in ('resync', 'recover', 'recovery', 'reshape'):
        value, state = f'OK · {action} in progress{" " + pct if pct else ""} — leave powered on', 'warn'
    else:
        value, state = f'OK · {working} working', 'ok'
    mismatch = a.get('mismatch_cnt') or 0
    if state == 'ok' and mismatch > 0 and action not in ('check', 'repair'):
        value, state = f'{value} · last check found {mismatch:,} mismatched sectors', 'warn'
    return Item(f'Array {device}', value, state, detail)


def _disk_item(d):
    smart = str(d.get('smart') or '').upper()
    problems = []
    temp = d.get('temperature_c')
    realloc, pending = d.get('reallocated_sectors') or 0, d.get('pending_sectors') or 0
    if smart == 'PASSED':
        value, state = 'PASSED', 'ok'
    elif smart == 'FAILED':
        value, state = 'FAILED — replace this drive', 'bad'
    else:
        value, state = 'unknown', 'warn'
    if isinstance(temp, (int, float)):
        value += f' · {temp:.0f} °C'
        if temp > DISK_HOT_C:
            problems.append(f'running hot (over {DISK_HOT_C} °C)')
    if realloc > 0:
        problems.append(f'{realloc:,} reallocated sector{"s" if realloc != 1 else ""}')
    if pending > 0:
        problems.append(f'{pending:,} pending sector{"s" if pending != 1 else ""}')
    if problems:
        value += ' · ' + ', '.join(problems)
        if state == 'ok':
            state = 'warn'
    hours = d.get('power_on_hours')
    detail = ' · '.join(str(x) for x in [d.get('model'), d.get('size'),
                                         f'{hours:,} power-on hours' if isinstance(hours, int) else '', d.get('detail')] if x)
    return Item(f"Drive {d.get('device') or '?'}", value, state, detail)


def _mdstat_degraded(md):
    if md['action'] in ('recovery', 'resync') and md['pct'] is not None:
        return f"DEGRADED (live /proc/mdstat) — REBUILDING, {md['pct']:.0f}% — do not power off"
    return 'DEGRADED (live /proc/mdstat)'


def _mdstat_item(md):
    shown = f"{md['counts']} {md['status']}".strip()
    progress = f" · {md['action']} {md['pct']:.0f}%" if md['action'] and md['pct'] is not None else ''
    detail = ' · '.join(x for x in [_level_name(md['level']), f'live /proc/mdstat: {shown}' if shown else '', progress.strip(' ·')] if x)
    if md['degraded']:
        return Item(f"Array /dev/{md['name']}", _mdstat_degraded(md), 'bad', detail)
    if md['state'] != 'active':
        return Item(f"Array /dev/{md['name']}", f"{md['state'] or 'unknown'} (live /proc/mdstat)", 'bad', detail)
    return Item(f"Array /dev/{md['name']}", 'OK (live /proc/mdstat)', 'ok', detail)


def raid_section():
    s = Section('RAID & drives')
    hw, problem = read_hw()
    live = read_mdstat()
    path = hw_status_path()
    if hw is None and problem == 'missing' and not live:
        s.items.append(Item('RAID and drive health', 'not set up on this host', 'muted', f'{path} (written by the configured status collector on the server)'))
        return s
    if hw is None:
        if problem == 'missing':
            s.items.append(Item('Drive details', 'detail file not set up on this host', 'muted', f'{path} — RAID rows below come from /proc/mdstat only'))
        else:
            s.items.append(Item('RAID / drive status file', problem, 'warn', str(path)))
        for md in live:
            s.items.append(_mdstat_item(md))
        s.note = 'RAID state read live from /proc/mdstat.'
        return s
    generated = _parse_iso(hw.get('generated'))
    if generated is None:
        s.items.append(Item('RAID / drive status file', 'no valid "generated" time', 'warn', str(path)))
    elif timezone.now() - generated > timedelta(minutes=HW_STALE_MINUTES):
        s.items.append(Item('RAID / drive status file', f'RAID/drive status not updated since {_local(generated)}', 'warn',
                            f'{path} (the configured status collector rewrites it every 10 min); rows below may be out of date'))
    live_by_name = {md['name']: md for md in live}
    matched = set()
    event = str(hw.get('event') or '').strip()
    for n, a in enumerate(hw.get('raid') or []):
        if not isinstance(a, dict):
            continue
        item = _array_item(a)
        md = live_by_name.get(os.path.basename(str(a.get('device') or '')))
        if md is None and len(live) == 1 and len(hw.get('raid') or []) == 1:
            md = live[0]                       # /dev/md/root style name in hw.json, md127 in /proc/mdstat
        if md is not None:
            matched.add(md['name'])
            shown = f"{md['counts']} {md['status']}".strip()
            if md['degraded'] and item.state != 'bad':
                item.value, item.state = _mdstat_degraded(md), 'bad'
            item.detail = ' · '.join(x for x in [item.detail, f'live /proc/mdstat: {shown}' if shown else ''] if x)
        if n == 0 and event:
            item.detail = ' · '.join(x for x in [item.detail, f'last mdadm event: {event}'] if x)
        s.items.append(item)
    for md in live:
        if md['name'] not in matched:
            s.items.append(_mdstat_item(md))
    for d in hw.get('disks') or []:
        if isinstance(d, dict):
            s.items.append(_disk_item(d))
    if not s.items:
        s.items.append(Item('RAID and drive health', 'no arrays or drives reported', 'muted', str(path)))
    written = f'last written {ago(generated)}' if generated else 'write time unknown'
    s.note = (f'Drive and RAID detail from {path} (root-written by the configured status collector every 10 min and on any RAID event; {written}). '
              'RAID state is also checked live against /proc/mdstat on every refresh.')
    return s


def software_section():
    s = Section('Software')
    s.items.append(Item('Python', sys.version.split()[0]))
    for name in PACKAGES:
        try:
            s.items.append(Item(name, metadata.version(name)))
        except metadata.PackageNotFoundError:
            s.items.append(Item(name, 'not installed', 'muted'))
    if connection.vendor == 'postgresql':
        try:
            with connection.cursor() as cur:
                cur.execute('SHOW server_version')
                s.items.append(Item('PostgreSQL', cur.fetchone()[0]))
        except Exception as exc:  # noqa: BLE001 — report, never crash the page
            s.items.append(Item('PostgreSQL', f'unavailable ({exc.__class__.__name__})', 'bad'))
    else:
        s.items.append(Item('Database engine', connection.vendor, 'muted'))
    for tool, args in [('nginx', ['nginx', '-v']), ('Borg', ['borg', '--version'])]:
        exe = shutil.which(args[0])
        if not exe:
            s.items.append(Item(tool, 'not found on PATH', 'muted'))
            continue
        code, out, err = run(args)
        s.items.append(Item(tool, (out or err).replace('nginx version: ', '').replace('borg ', '').strip() or 'installed'))
    code, out, _ = run(['git', '-C', str(APP_DIR), 'log', '-1', '--format=%h %ad %s', '--date=short'])
    if code == 0 and out:
        s.items.append(Item('Application build', out))
    else:
        s.items.append(Item('Application build', 'no Git metadata on this host', 'muted'))
    return s


def application_section():
    s = Section('Application')
    code, out, _ = run(['systemctl', 'show', APP_UNIT, '--property=LoadState,ActiveState,SubState,ActiveEnterTimestamp,NRestarts,MainPID'])
    props = dict(line.split('=', 1) for line in out.splitlines() if '=' in line) if code == 0 else {}
    if props and props.get('LoadState') == 'not-found':
        s.items.append(Item(f'{APP_UNIT} service', 'not installed on this host (development server)', 'muted'))
    elif props:
        active = props.get('ActiveState') == 'active'
        s.items.append(Item(f'{APP_UNIT} service', f"{props.get('ActiveState', '?')} ({props.get('SubState', '?')})", 'ok' if active else 'bad',
                            f"since {props.get('ActiveEnterTimestamp', '?')} · restarts: {props.get('NRestarts', '?')}"))
    else:
        s.items.append(Item(f'{APP_UNIT} service', 'systemd not available (development server)', 'muted'))
    try:
        started = time.perf_counter()
        with connection.cursor() as cur:
            if connection.vendor == 'postgresql':
                cur.execute('SELECT current_database(), pg_database_size(current_database()), (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())')
                name, size, sessions = cur.fetchone()
            else:
                name, size, sessions = connection.settings_dict.get('NAME', connection.vendor), 0, 1
            cur.execute("SELECT count(*) FROM sales_document WHERE kind='sale' AND status='completed'")
            receipts = cur.fetchone()[0]
            cur.execute('SELECT count(*) FROM sales_item')
            items = cur.fetchone()[0]
            cur.execute('SELECT count(*) FROM customers_customer')
            customers = cur.fetchone()[0]
        ms = (time.perf_counter() - started) * 1000
        s.items.append(Item('Database', f'{name} · {human_bytes(size)}', 'ok', f'{sessions} connection{"s" if sessions != 1 else ""} · queries answered in {ms:.0f} ms'))
        s.items.append(Item('Records', f'{receipts:,} sales receipts · {items:,} items · {customers:,} customers'))
    except Exception as exc:  # noqa: BLE001
        s.items.append(Item('Database', f'unavailable ({exc.__class__.__name__})', 'bad'))
    for path in CERT_PATHS:
        s.items.append(cert_item(path))
    from sales.printing import testing_marks
    s.items.append(Item('Testing marks on printed documents', 'On' if testing_marks() else 'Off'))
    return s


def _cert_state(expires=None, days=None):
    if expires is not None:
        days = (expires - timezone.now()).days
    state = 'ok' if days > 30 else ('warn' if days > 7 else 'bad')
    when = f'expires {expires:%-m/%-d/%Y} ({days} days)' if expires is not None else f'expires in {days} days'
    return when, state, days


def cert_item(path):
    """HTTPS certificate expiry: openssl when the app user can read the file, else the root-written hw.json."""
    code, out, _ = run(['openssl', 'x509', '-enddate', '-noout', '-in', str(path)])
    if code == 0 and out.startswith('notAfter='):
        try:
            expires = datetime.strptime(out[len('notAfter='):].strip(), '%b %d %H:%M:%S %Y %Z').replace(tzinfo=dt_timezone.utc)
        except ValueError:
            return Item('HTTPS certificate', out, 'muted')
        value, state, _ = _cert_state(expires)
        return Item('HTTPS certificate', value, state, f'renew before {expires - timedelta(days=30):%-m/%-d/%Y}')
    hw, _ = read_hw()
    cert = hw.get('cert') if isinstance(hw, dict) else None
    if isinstance(cert, dict):
        expires = _parse_iso(cert.get('not_after')) if cert.get('not_after') else None
        days = cert.get('days_left')
        if expires is not None or isinstance(days, int):
            value, state, _ = _cert_state(expires, days)
            renew = f'renew before {expires - timedelta(days=30):%-m/%-d/%Y} · ' if expires is not None else ''
            return Item('HTTPS certificate', value, state, f'{renew}from the server status file ({cert.get("path") or path})')
    if _exists(path) or _exists(Path('/etc/letsencrypt')):
        return Item('HTTPS certificate', 'not readable by the app user', 'muted', str(path))
    return Item('HTTPS certificate', 'not on this host (development)', 'muted')


def _timer_props(unit):
    """(timer, service, systemd_ok): timer is {} when the timer doesn't exist or systemctl failed; systemd_ok says which."""
    code, out, _ = run(['systemctl', 'show', f'{unit}.timer', '--property=LoadState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime'])
    systemd_ok = code == 0
    timer = dict(line.split('=', 1) for line in out.splitlines() if '=' in line) if systemd_ok else {}
    if timer.get('LoadState') == 'not-found':
        timer = {}
    code, out, _ = run(['systemctl', 'show', f'{unit}.service', '--property=ActiveState,Result,ExecMainExitTimestamp,ExecMainStatus'])
    service = dict(line.split('=', 1) for line in out.splitlines() if '=' in line) if code == 0 else {}
    return timer, service, systemd_ok


def hourly_label():
    override = getattr(settings, 'BACKUP_HOURLY_LABEL', '')
    if override:
        return override
    return HOURLY_LABEL


def _backup_item(description, timer_active, result, status, detail):
    if timer_active != 'active':
        state, value = 'bad', 'timer is not active'
    elif result and result != 'success':
        state, value = 'bad', f'last run failed ({result}, exit {status})'
    else:
        state, value = 'ok', 'scheduled · last run succeeded' if result == 'success' else 'scheduled'
    return Item(description, value, state, detail)


def _snapshot_age_state(when):
    age_h = (timezone.now() - when).total_seconds() / 3600
    return 'ok' if age_h < 30 else ('warn' if age_h < 54 else 'bad')


def _nightly_from_hw():
    """The newest nightly snapshot as reported by the root-written hw.json (when the snapshot folder is root-only)."""
    hw, problem = read_hw()
    backup = hw.get('backup') if isinstance(hw, dict) and isinstance(hw.get('backup'), dict) else {}
    snaps = backup.get('nightly_snapshots') if isinstance(backup.get('nightly_snapshots'), dict) else None
    if snaps is None:
        return None
    newest = snaps.get('newest') if isinstance(snaps.get('newest'), dict) else None
    if not snaps.get('count') or not newest:
        return Item('Newest nightly snapshot', 'none yet', 'warn', 'from the server status file')
    when = _parse_iso(newest.get('mtime'))
    if when is None:
        return Item('Newest nightly snapshot', newest.get('name') or 'unknown', 'warn', 'from the server status file (no time)')
    state = _snapshot_age_state(when)
    if problem and state == 'ok':
        state = 'warn'  # a stale status file can't vouch for a fresh snapshot
    size = newest.get('bytes')
    size_text = human_bytes(size) if isinstance(size, (int, float)) else 'size unknown'
    return Item('Newest nightly snapshot', f"{newest.get('name')} · {ago(when)}", state,
                f"{size_text} · {snaps.get('count')} snapshot{'s' if snaps.get('count') != 1 else ''} kept · from the server status file")


def _nightly_snapshot_item():
    try:
        snaps = sorted(p for p in NIGHTLY_ROOT.iterdir() if p.name.startswith('nightly-'))
    except PermissionError:
        from_hw = _nightly_from_hw()
        if from_hw is not None:
            return from_hw
        return Item('Newest nightly snapshot', 'folder not readable by the app user', 'muted', f'{NIGHTLY_ROOT} is root-only (mode 700); timer status above still reflects success or failure.')
    except FileNotFoundError:
        return Item('Newest nightly snapshot', 'no nightly backups on this host', 'muted', str(NIGHTLY_ROOT))
    if not snaps:
        return Item('Newest nightly snapshot', 'none yet', 'warn')
    newest = snaps[-1]
    when = datetime.fromtimestamp(newest.stat().st_mtime, tz=dt_timezone.utc)
    age_h = (timezone.now() - when).total_seconds() / 3600
    state = 'ok' if age_h < 30 else ('warn' if age_h < 54 else 'bad')
    size = sum(f.stat().st_size for f in newest.rglob('*') if f.is_file())
    return Item('Newest nightly snapshot', f'{newest.name} · {ago(when)}', state, f'{human_bytes(size)} · {len(snaps)} snapshots kept')


def backups_section():
    s = Section('Backups')
    # Nightly local snapshot, when configured on this host.
    timer, service, systemd_ok = _timer_props(NIGHTLY_UNIT)
    has_nightly = bool(timer)
    if timer:
        last = timer.get('LastTriggerUSec', '') or (f"run by hand {service['ExecMainExitTimestamp']}" if service.get('ExecMainExitTimestamp') else 'never')
        s.items.append(_backup_item(NIGHTLY_LABEL, timer.get('ActiveState'), service.get('Result', ''), service.get('ExecMainStatus', ''),
                                    f"last: {last} · next: {timer.get('NextElapseUSecRealtime', '') or 'not scheduled'}"))
        s.items.append(_nightly_snapshot_item())
    elif not systemd_ok and _exists(NIGHTLY_ROOT):
        has_nightly = True
        s.items.append(Item(NIGHTLY_LABEL, 'timer status not readable (systemctl unavailable)', 'muted', f'{NIGHTLY_UNIT}.timer'))
        s.items.append(_nightly_snapshot_item())
    else:
        s.items.append(Item('Nightly local snapshot', 'No local nightly snapshot on this server (hourly off-site backups only)', 'muted', f'no {NIGHTLY_UNIT}.timer on this host'))
    # Hourly off-site Borg job.
    label = hourly_label()
    timer, service, systemd_ok = _timer_props(HOURLY_UNIT)
    hw_backup = None
    if not systemd_ok:
        hw, _ = read_hw()
        hw_backup = hw.get('backup') if isinstance(hw, dict) and isinstance(hw.get('backup'), dict) else None
    has_hourly = True
    if timer:
        s.items.append(_backup_item(label, timer.get('ActiveState'), service.get('Result', ''), service.get('ExecMainStatus', ''),
                                    f"last: {timer.get('LastTriggerUSec', '') or 'never'} · next: {timer.get('NextElapseUSecRealtime', '') or 'not scheduled'}"))
    elif hw_backup and isinstance(hw_backup.get(HOURLY_UNIT), dict):
        svc = hw_backup[HOURLY_UNIT]
        s.items.append(_backup_item(label, hw_backup.get('timer_active'), svc.get('Result', ''), svc.get('ExecMainStatus', ''),
                                    f"last: {svc.get('ExecMainStartTimestamp') or 'never'} · from the server status file"))
    else:
        has_hourly = False
        s.items.append(Item(label, 'timer not found on this host', 'muted', f'{HOURLY_UNIT}.timer'))
    parts = ['Nightly snapshots live on this server.' if has_nightly else 'This server keeps no local nightly snapshot.']
    if has_hourly:
        parts.append('The hourly Borg job copies to the configured encrypted repositories.')
    parts.append('A green status means the last scheduled run finished without error; it does not by itself prove a restore has been rehearsed.')
    s.note = ' '.join(parts)
    return s


# Show backups first, followed by RAID and drive health.
# NUT's `upsc` uses an unprivileged local connection; no sudo or status file is needed.
UPS_NAME = getattr(settings, 'SERVER_UPS_NAME', 'ups@localhost')


def read_ups(name=UPS_NAME):
    """{variable: value} from `upsc <name>`, or None when NUT is not installed / the UPS is not reporting."""
    if shutil.which('upsc') is None:
        return 'no-nut'
    code, out, _ = run(['upsc', name], timeout=4)
    if code != 0 or not out:
        return None
    values = {}
    for line in out.splitlines():
        key, sep, value = line.partition(':')
        if sep:
            values[key.strip()] = value.strip()
    return values or None


def _runtime_text(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return 'runtime unknown'
    return f'{seconds // 60} min' if seconds >= 120 else f'{seconds} s'


def power_section():
    s = Section('Power (UPS)')
    ups = read_ups()
    if ups == 'no-nut':  # dev machines and previews: NUT isn't installed, so this isn't a fault to show in red
        s.items.append(Item('Battery backup', 'no UPS software on this machine', 'muted', 'NUT (upsc) is not installed.'))
        return s
    if ups is None:
        s.items.append(Item('Battery backup', 'UPS not reporting', 'bad',
                            f'`upsc {UPS_NAME}` gave no reading. Check the USB cable and that nut-server is running.'))
        return s
    flags = ups.get('ups.status', '').split()
    model = (ups.get('ups.model') or 'UPS').strip()
    volts = ups.get('input.voltage')
    volts_text = f' · input {float(volts):.0f} V' if volts not in (None, '') else ''
    if 'LB' in flags:
        state, value = 'bad', 'LOW BATTERY — the server will shut down soon'
    elif 'OB' in flags:
        state, value = 'warn', 'On battery — power is out'
    elif 'OL' in flags:
        state, value = 'ok', 'On line power'
    else:
        state, value = 'bad', f"status unknown ({ups.get('ups.status') or 'no status'})"
    extra = ' · charging' if 'CHRG' in flags else (' · discharging' if 'DISCHRG' in flags else '')
    s.items.append(Item('Battery backup', value + extra, state, f'{model}{volts_text} · NUT {UPS_NAME}'))
    charge = ups.get('battery.charge')
    try:
        pct = float(charge)
    except (TypeError, ValueError):
        s.items.append(Item('Battery', 'charge unknown', 'warn', 'battery.charge not reported'))
    else:
        cstate = 'ok' if pct >= 50 else ('warn' if pct >= 20 else 'bad')
        s.items.append(Item('Battery', f'{pct:.0f}% · about {_runtime_text(ups.get("battery.runtime"))} of runtime', cstate,
                            'warning under 50%, attention under 20%'))
    load = ups.get('ups.load')
    if load not in (None, ''):
        try:
            s.items.append(Item('Load', f'{float(load):.0f}% of the UPS capacity', 'ok' if float(load) < 80 else 'warn'))
        except ValueError:
            pass
    return s


SECTION_BUILDERS = [('Power (UPS)', 'power_section'), ('Backups', 'backups_section'), ('RAID & drives', 'raid_section'), ('System', 'system_section'),
                    ('Memory', 'memory_section'), ('Storage', 'storage_section'), ('Software', 'software_section'),
                    ('Application', 'application_section')]


def gather():
    sections = []
    for title, name in SECTION_BUILDERS:
        try:
            sections.append(globals()[name]())
        except Exception as exc:  # noqa: BLE001 — a probe failure is itself a status to show, not a crash
            sections.append(Section(title, [Item('Status check failed', f'{exc.__class__.__name__}: {exc}', 'warn')]))
    return sections


CACHE_KEY = 'server_status_v1'
CACHE_SECONDS = 60


def cached(refresh=False):
    """(sections, state, summary, gathered_at) — reused for up to a minute so the menubar health dot is cheap."""
    from django.core.cache import cache
    result = None if refresh else cache.get(CACHE_KEY)
    if result is None:
        sections = gather()
        state, summary = overall(sections)
        result = (sections, state, summary, timezone.now())
        cache.set(CACHE_KEY, result, CACHE_SECONDS)
    return result


def health(request):
    """Template context processor: {{ server_health }} = ok | warn | bad, only while Admin Mode is on."""
    from .admin_mode import is_on
    if not is_on(request):
        return {}
    try:
        return {'server_health': cached()[1]}
    except Exception:  # noqa: BLE001 — never break page rendering over a status probe
        return {'server_health': 'warn'}


def overall(sections):
    states = [i.state for s in sections for i in s.items]
    if 'bad' in states:
        return 'bad', 'Attention needed'
    if 'warn' in states:
        return 'warn', 'Warnings'
    return 'ok', 'All checks OK'
