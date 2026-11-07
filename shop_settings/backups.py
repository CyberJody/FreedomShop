# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verified local backups and a two-step, reversible restore. No network services or threads.

Only configured live destinations may be written outside BACKUP_DIR. Backup identifiers
are stamps, never paths. Filesystem locks coordinate the web app and management commands.
"""
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import load_backend
from django.utils import timezone

from .models import Backup, BackupSchedule, Business, RestoreLog

STAMP = re.compile(r'\A[0-9]{8}-[0-9]{6}\Z')
PAYLOAD = ('db.dump', 'media.tar.gz', 'settings.tar.gz', 'MANIFEST.json')
FILES = (*PAYLOAD, 'SHA256SUMS')
FORMAT = 'freedomshop-backup/1'


class BackupError(Exception):
    pass


class RecoveryRequired(BackupError):
    """Database promotion could not prove that the former live database is active."""


def live_connection():
    return connections['default']


def _plain_path(path):
    """Reject symlinks at every component, including a not-yet-created leaf."""
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise BackupError('A configured path contains a symbolic link.')
    return path


def _configured(path):
    """Settings may point through links (a mounted data disk); everything below the real path is link-free."""
    return Path(os.path.realpath(path))


def backup_root():
    data = getattr(settings, 'DATA_DIR', Path(getattr(settings, 'BASE_DIR', Path.cwd())) / 'data')
    root = _configured(getattr(settings, 'BACKUP_DIR', Path(data) / 'backups'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def checked_path(stamp):
    if not isinstance(stamp, str) or not STAMP.fullmatch(stamp):
        raise BackupError('Invalid backup stamp; use YYYYMMDD-HHMMSS.')
    try:
        datetime.strptime(stamp, '%Y%m%d-%H%M%S')
    except ValueError as exc:
        raise BackupError('Invalid backup date.') from exc
    root = backup_root()
    path = _plain_path(root / stamp)
    if path.resolve().parent != root:
        raise BackupError('Backup path escapes BACKUP_DIR.')
    return path


def control_path(name):
    if name not in ('.lock', '.requests', 'MAINTENANCE', 'GENERATION', '.restore'):
        raise BackupError('Invalid control path.')
    return _plain_path(backup_root() / name)


def ensure_available():
    if control_path('MAINTENANCE').exists():
        raise BackupError('Maintenance is active; review the restore journal before another backup operation.')


@contextmanager
def file_lock(name='.lock', shared=False, blocking=False):
    path = control_path(name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(fd, operation | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise BackupError('Another backup or restore is running. Try again when it finishes.') from exc
        yield
    finally:
        os.close(fd)


def _json(path, value):
    path = _plain_path(path)
    temporary = _plain_path(path.with_name(path.name + '.tmp'))
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise BackupError('Could not write backup/restore metadata; check the journal disk.') from exc


def sha256(path):
    digest = hashlib.sha256()
    with _plain_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_download(backup, stream=None):
    """Deterministic uncompressed tar; HTTP spools to disk then streams FileResponse."""
    import tarfile
    digest = hashlib.sha256()

    class Writer:
        def write(self, data):
            digest.update(data)
            if stream is not None:
                stream.write(data)
            return len(data)

    path = checked_path(backup.stamp)
    with tarfile.open(fileobj=Writer(), mode='w|', format=tarfile.USTAR_FORMAT) as archive:
        for name in FILES:
            source = _plain_path(path / name)
            info = tarfile.TarInfo(f'{backup.stamp}/{name}')
            info.size, info.mode = source.stat().st_size, 0o600
            with source.open('rb') as content:
                archive.addfile(info, content)
    return digest.hexdigest()


def _media_roots():
    roots = {'media': _configured(settings.MEDIA_ROOT)}
    data = getattr(settings, 'DATA_DIR', None)
    uploads = getattr(settings, 'BACKUP_UPLOAD_ROOT', Path(data) / 'uploads' if data else None)
    if uploads:
        roots['uploads'] = _configured(uploads)
    tracker = getattr(settings, 'TRACKER_UPLOAD_DIR', None)
    if tracker and not any(_configured(tracker).is_relative_to(p) for p in roots.values()):
        roots['tracker'] = _configured(tracker)
    result = {}
    for label, path in roots.items():
        if not any(path.is_relative_to(p) for p in result.values()):
            if any(p.is_relative_to(path) for p in result.values()):
                raise BackupError('Configured attachment directories overlap.')
            result[label] = path
    return result


def _settings_files():
    configured = getattr(settings, 'BACKUP_SETTINGS_FILES', None)
    if configured is None:
        spec = importlib.util.find_spec(os.environ.get('DJANGO_SETTINGS_MODULE', 'config.settings'))
        path = Path(spec.origin).absolute() if spec and spec.origin else None
        repo = Path(getattr(settings, 'BASE_DIR', Path(__file__).resolve().parent.parent)).resolve()
        configured = [path] if path and not path.resolve().is_relative_to(repo) else []
    return [_configured(p) for p in configured]


def _destinations():
    roots, files = _media_roots(), _settings_files()
    destinations = [*roots.values(), *files]
    root = backup_root()
    for path in destinations:
        if path == Path(path.anchor) or root.is_relative_to(path) or path.is_relative_to(root):
            raise BackupError('Live files and BACKUP_DIR must not overlap.')
    for i, path in enumerate(destinations):
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in destinations[:i]):
            raise BackupError('Configured live file destinations overlap.')
    config = live_connection().settings_dict
    if _engine(config) == 'sqlite' and str(config['NAME']) != ':memory:' and not str(config['NAME']).startswith('file:'):
        database = _configured(config['NAME'])
        if any(database.is_relative_to(p) for p in [root, *destinations]):
            raise BackupError('The live SQLite database overlaps a backup or attachment/settings destination.')
    return roots, files


def _pg_args(config):
    args = []
    for key, option in [('HOST', '--host'), ('PORT', '--port'), ('USER', '--username')]:
        if config.get(key):
            args.extend([option, str(config[key])])
    return args


def _run(argv, config):
    env = os.environ.copy()
    if config.get('PASSWORD'):
        env['PGPASSWORD'] = str(config['PASSWORD'])
    try:
        return subprocess.run(argv, check=True, capture_output=True, env=env,
                              timeout=getattr(settings, 'BACKUP_COMMAND_TIMEOUT', 3600))
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not put credentials, server addresses or SQL contents in a page/log.
        raise BackupError(f'{argv[0]} failed; check the database tools and role privileges.') from exc


def _engine(config):
    if config['ENGINE'].endswith('sqlite3'):
        return 'sqlite'
    if config['ENGINE'].endswith('postgresql'):
        return 'postgresql'
    raise BackupError('Only SQLite and PostgreSQL backups are supported.')


def _schema(conn):
    with conn.cursor() as cursor:
        cursor.execute('SELECT app, name FROM django_migrations ORDER BY app, name')
        return [list(row) for row in cursor.fetchall()]


def _dump(path, config):
    if _engine(config) == 'postgresql':
        _run(['pg_dump', '-Fc', *_pg_args(config), '--file', str(path), '--dbname', str(config['NAME'])], config)
    else:
        conn = live_connection()
        conn.ensure_connection()
        if conn.in_atomic_block:
            raise BackupError('SQLite backup requires a connection outside a transaction.')
        with sqlite3.connect(path) as target:
            conn.connection.backup(target)
    os.chmod(path, 0o600)


def _add_tree(archive, source, name):
    # No links, devices, sockets or implicit recursive traversal by tarfile.
    import tarfile
    if not source.exists():
        info = tarfile.TarInfo(name)
        info.type, info.mode = tarfile.DIRTYPE, 0o700
        archive.addfile(info)
        return
    paths = [source, *sorted(source.rglob('*'))] if source.is_dir() else [source]
    for path in paths:
        _plain_path(path)
        if not (path.is_file() or path.is_dir()):
            raise BackupError('Attachments/settings contain a non-regular file.')
        arcname = name if path == source else f'{name}/{path.relative_to(source).as_posix()}'
        archive.add(path, arcname=arcname, recursive=False, filter=_tar_metadata)


def _tar_metadata(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    info.mode = 0o700 if info.isdir() else 0o600
    info.pax_headers = {}
    return info


def _tar_members(path, allowed):
    import tarfile
    with tarfile.open(_plain_path(path), 'r:gz') as archive:
        seen = set()
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if (not parts or member.name.startswith('/') or '\\' in member.name or '..' in parts
                    or parts[0] not in allowed or not (member.isfile() or member.isdir())
                    or member.name in seen):
                raise BackupError('Unsafe or duplicate archive member.')
            seen.add(member.name)
        return seen


def _extract(path, target, allowed):
    import tarfile
    _tar_members(path, allowed)
    target.mkdir(mode=0o700)
    with tarfile.open(path, 'r:gz') as archive:
        # All names/types checked above; write only fresh files below a fresh directory.
        for member in archive:
            dest = _plain_path(target / member.name)
            if not dest.is_relative_to(target):
                raise BackupError('Archive path escapes the restore copy.')
            if member.isdir():
                dest.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with dest.open('xb') as output, archive.extractfile(member) as source:
                    os.chmod(dest, 0o600)
                    shutil.copyfileobj(source, output)


def _new_stamp():
    now = timezone.now()
    for offset in range(86400):
        stamp = (now + timedelta(seconds=offset)).strftime('%Y%m%d-%H%M%S')
        if not checked_path(stamp).exists() and not Backup.objects.filter(stamp=stamp).exists():
            return stamp
    raise BackupError('No unused backup stamp available.')


def _app_version():
    configured = getattr(settings, 'APP_VERSION', None)
    if configured is not None:
        return str(configured)
    try:
        # Packaging supplies this module at merge; source-only checkouts are development builds.
        from freedomshop.__version__ import __version__
        return __version__
    except ImportError:
        return 'development'


def create_backup(reason='manual', keep=None):
    """keep=N also applies scheduled-backup retention once this backup has verified (CLI --keep)."""
    if keep is not None and keep < 1:
        raise BackupError('Keep at least one scheduled backup.')
    with file_lock():
        ensure_available()
        backup = _create_backup(reason)
        if keep is not None:
            prune_scheduled(keep)
        return backup


def _create_backup(reason):
    import tarfile
    if reason not in ('manual', 'scheduled', 'safety'):
        raise BackupError('Invalid backup reason.')
    roots, settings_files = _destinations()
    stamp = _new_stamp()
    path = checked_path(stamp)
    path.mkdir(mode=0o700)
    backup = Backup.objects.create(stamp=stamp, reason=reason, path=str(path))
    step = 'database dump'
    try:
        config = live_connection().settings_dict
        _dump(path / 'db.dump', config)
        step = 'attachment archive'
        with tarfile.open(path / 'media.tar.gz', 'w:gz') as archive:
            for label, source in roots.items():
                _add_tree(archive, source, label)
        step = 'settings archive'
        with tarfile.open(path / 'settings.tar.gz', 'w:gz') as archive:
            for index, source in enumerate(settings_files):
                if not source.is_file():
                    raise BackupError('A configured settings file is missing.')
                _add_tree(archive, source, f'settings/{index:04d}')
        step = 'manifest'
        manifest = {'format': FORMAT, 'stamp': stamp, 'reason': reason,
                    'app_version': _app_version(),
                    'database': _engine(config), 'schema': _schema(live_connection()),
                    'media_roots': list(roots),
                    'settings_files': [p.name for p in settings_files],
                    'sizes': {name: (path / name).stat().st_size for name in PAYLOAD[:-1]}}
        _json(path / 'MANIFEST.json', manifest)
        with (path / 'SHA256SUMS').open('x') as output:
            for name in PAYLOAD:
                output.write(f'{sha256(path / name)}  {name}\n')
        for name in FILES:
            os.chmod(path / name, 0o600)
            with (path / name).open('rb') as stream:
                os.fsync(stream.fileno())
        step = 'verification'
        verify_backup(backup)
    except Exception as exc:
        backup.status = 'failed'
        backup.error = f'Stopped at {step}: {_safe_error(exc)}'
        backup.size = sum(p.stat().st_size for p in path.iterdir() if p.is_file() and not p.is_symlink())
        backup.save(update_fields=['status', 'error', 'size'])
        raise BackupError(backup.error) from exc
    return backup


def _safe_error(exc):
    return str(exc) if isinstance(exc, BackupError) else f'{type(exc).__name__}; operation failed.'


def verify_backup(backup):
    try:
        path = checked_path(backup.stamp)
        if Path(backup.path) != path or {p.name for p in path.iterdir()} != set(FILES):
            raise BackupError('Backup path or file set does not match the expected layout.')
        for name in FILES:
            if not _plain_path(path / name).is_file():
                raise BackupError('Backup contains a non-regular file.')
        lines = (path / 'SHA256SUMS').read_text().splitlines()
        expected = {}
        for line in lines:
            match = re.fullmatch(r'([a-f0-9]{64})  (db\.dump|media\.tar\.gz|settings\.tar\.gz|MANIFEST\.json)', line)
            if not match or match[2] in expected:
                raise BackupError('Invalid SHA256SUMS.')
            expected[match[2]] = match[1]
        if set(expected) != set(PAYLOAD):
            raise BackupError('Incomplete SHA256SUMS.')
        for name, digest in expected.items():
            if sha256(path / name) != digest:
                raise BackupError(f'SHA256 mismatch: {name}.')
        manifest = json.loads((path / 'MANIFEST.json').read_text())
        if (manifest['format'] != FORMAT or manifest['stamp'] != backup.stamp
                or manifest['reason'] != backup.reason or manifest['database'] not in ('sqlite', 'postgresql')):
            raise BackupError('Incompatible manifest.')
        if any((path / name).stat().st_size != manifest['sizes'][name] for name in PAYLOAD[:-1]):
            raise BackupError('Manifest sizes do not match.')
        roots = manifest['media_roots']
        if not isinstance(roots, list) or not set(roots) <= {'media', 'uploads', 'tracker'}:
            raise BackupError('Invalid attachment roots.')
        _tar_members(path / 'media.tar.gz', set(roots))
        members = _tar_members(path / 'settings.tar.gz', {'settings'})
        if members != {f'settings/{i:04d}' for i in range(len(manifest['settings_files']))}:
            raise BackupError('Settings archive does not match the manifest.')
        if manifest['database'] == 'postgresql':
            _run(['pg_restore', '--list', str(path / 'db.dump')], live_connection().settings_dict)
        else:
            with sqlite3.connect((path / 'db.dump').as_uri() + '?mode=ro', uri=True) as conn:
                if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise BackupError('SQLite integrity check failed.')
        backup.status, backup.error = 'verified', ''
        backup.size = sum((path / name).stat().st_size for name in FILES)
        backup.download_sha256 = write_download(backup)
        backup.save(update_fields=['status', 'error', 'size', 'download_sha256'])
        return manifest
    except Exception as exc:
        backup.status, backup.error = 'failed', f'Verification failed: {_safe_error(exc)}'
        backup.save(update_fields=['status', 'error'])
        raise BackupError(backup.error) from exc


def find_backup(stamp):
    """Catalog row for a stamp. A folder copied into BACKUP_DIR without a row (a fresh or
    replacement install, or another disk) is adopted, verified, using its manifest's reason."""
    path = checked_path(stamp)
    row = Backup.objects.filter(stamp=stamp).first()
    if row is not None:
        return row
    if not path.is_dir():
        raise BackupError('No backup with that stamp.')
    try:
        reason = json.loads(_plain_path(path / 'MANIFEST.json').read_text())['reason']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError('The backup folder has no readable manifest.') from exc
    if reason not in ('manual', 'scheduled', 'safety'):
        raise BackupError('Invalid backup reason.')
    row = Backup.objects.create(stamp=stamp, reason=reason, path=str(path))
    verify_backup(row)
    return row


def list_backups():
    # Model paths are never trusted as inputs to filesystem access.
    ensure_available()
    return Backup.objects.all()


def _stage_path(stamp):
    checked_path(stamp)
    parent = control_path('.restore')
    parent.mkdir(mode=0o700, exist_ok=True)
    return _plain_path(parent / stamp)


def _sanity_database(config, manifest):
    backend = load_backend(config['ENGINE'])
    conn = backend.DatabaseWrapper(copy.deepcopy(config), alias='backup_sanity')
    # MigrationRecorder uses ORM queries against the wrapper's alias.
    connections[conn.alias] = conn
    try:
        conn.ensure_connection()
        tables = conn.introspection.table_names()
        if 'shop_settings_business' not in tables:
            raise BackupError('The restore copy has no business table.')
        with conn.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM shop_settings_business')
            businesses = cursor.fetchone()[0]
            if config['ENGINE'].endswith('sqlite3'):
                cursor.execute('PRAGMA integrity_check')
                if cursor.fetchall() != [('ok',)]:
                    raise BackupError('The restore copy failed its integrity check.')
                cursor.execute('PRAGMA foreign_key_check')
                if cursor.fetchone():
                    raise BackupError('The restore copy has broken references.')
        if businesses != 1:
            raise BackupError('The restore copy must contain exactly one business row.')
        executor = MigrationExecutor(conn)
        executor.loader.check_consistent_history(conn)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise BackupError('The restore copy needs migrations; use matching application code first.')
        if _schema(conn) != manifest['schema']:
            raise BackupError('Applied migrations differ from the backup manifest.')
        return {'table_count': len(tables), 'business_rows': businesses, 'migrations_current': True}
    finally:
        conn.close()
        del connections[conn.alias]


def _pg_name(config, kind, stamp):
    name = f'{config["NAME"]}_{kind}_{stamp}'
    if len(name.encode()) > 63:
        raise BackupError('Database name is too long for safe restore names (maximum 39 bytes for the live name).')
    return name


def _tree_hashes(path):
    hashes = {}
    for p in sorted(path.rglob('*')):
        _plain_path(p)
        if p.is_file():
            hashes[p.relative_to(path).as_posix()] = sha256(p)
        elif not p.is_dir():
            raise BackupError('Non-regular file in restore copy.')
    return hashes


def _check_bindings(manifest):
    roots, files = _destinations()
    if list(roots) != manifest['media_roots'] or [p.name for p in files] != manifest['settings_files']:
        raise BackupError('Configured attachment/settings destinations differ from this backup.')
    return roots, files


def restore_to_copy(backup):
    with file_lock():
        ensure_available()
        stage = _stage_path(backup.stamp)
        step = 'verify source'
        report = {'stamp': backup.stamp, 'status': 'failed', 'step': step}
        try:
            manifest = verify_backup(backup)
            _check_bindings(manifest)
            config = copy.deepcopy(live_connection().settings_dict)
            if _engine(config) != manifest['database']:
                raise BackupError('The backup uses a different database engine.')
            step = 'create new copy'
            if stage.exists():
                raise BackupError('A restore copy already exists; review its report before switching.')
            stage.mkdir(mode=0o700)
            if manifest['database'] == 'postgresql':
                name = _pg_name(config, 'restore', backup.stamp)
                _run(['createdb', *_pg_args(config), '--template=template0', '--', name], config)
                step = 'load database copy'
                _run(['pg_restore', *_pg_args(config), '--exit-on-error', '--no-owner', '--no-privileges',
                      '--dbname', name, str(checked_path(backup.stamp) / 'db.dump')], config)
                config['NAME'] = name
                report['database_copy'] = name
            else:
                config['NAME'] = str(stage / 'db.sqlite3')
                with sqlite3.connect(checked_path(backup.stamp) / 'db.dump') as source:
                    with sqlite3.connect(config['NAME']) as dest:
                        source.backup(dest)
                os.chmod(config['NAME'], 0o600)
                report['database_copy'] = 'db.sqlite3'
            step = 'check database copy'
            report.update(_sanity_database(config, manifest))
            step = 'stage attachments and settings'
            _extract(checked_path(backup.stamp) / 'media.tar.gz', stage / 'media', set(manifest['media_roots']))
            _extract(checked_path(backup.stamp) / 'settings.tar.gz', stage / 'settings', {'settings'})
            report.update(status='ready', step='copy verified', files=_tree_hashes(stage),
                          source_sha256=sha256(checked_path(backup.stamp) / 'SHA256SUMS'))
        except Exception as exc:
            report.update(status='failed', step=step, error=_safe_error(exc))
        backup.restore_report = report
        backup.save(update_fields=['restore_report'])
        _json(stage / 'REPORT.json', report) if stage.exists() else None
        RestoreLog.objects.create(backup=backup, action='copy', status=report['status'], step=report['step'],
                                  report=report, error=report.get('error', ''))
        return report


def discard_copy(backup):
    """Drop a restore copy (PostgreSQL database and staging folder) so the stamp can be restored again.

    Refused while a switch-over needs recovery: its journal and copies are the operator's evidence."""
    with file_lock():
        ensure_available()
        report = backup.restore_report or {}
        if report.get('status') == 'recovery_required':
            raise BackupError('This restore needs operator recovery; keep its copy and journal.')
        stage = _stage_path(backup.stamp)
        config = live_connection().settings_dict
        if _engine(config) == 'postgresql':
            # After a completed switch this name no longer exists: the copy became the live database.
            _run(['dropdb', *_pg_args(config), '--if-exists', '--', _pg_name(config, 'restore', backup.stamp)], config)
        if stage.exists():
            _remove(stage)
        backup.restore_report = {}
        backup.save(update_fields=['restore_report'])
        RestoreLog.objects.create(backup=backup, action='discard', status='complete', step='copy discarded', report=report)


def _copy_report(backup, manifest):
    report = backup.restore_report
    stage = _stage_path(backup.stamp)
    if report.get('status') != 'ready' or not stage.is_dir():
        raise BackupError('Restore into a copy and obtain a successful report first.')
    hashes = _tree_hashes(stage)
    hashes.pop('REPORT.json', None)
    if hashes != report['files'] or sha256(checked_path(backup.stamp) / 'SHA256SUMS') != report['source_sha256']:
        raise BackupError('The source or restore copy changed after verification; switch-over refused.')
    config = copy.deepcopy(live_connection().settings_dict)
    config['NAME'] = (_pg_name(config, 'restore', backup.stamp) if manifest['database'] == 'postgresql'
                      else str(stage / 'db.sqlite3'))
    _sanity_database(config, manifest)
    return stage, config


def _prepare_files(stage, manifest, stamp):
    """Returns (prepared renames, settings basenames kept as live).

    Settings files equal to the backup are left alone. A changed one whose folder the app
    cannot write (root-owned /etc on native installs) stays as it is and is reported."""
    roots, files = _check_bindings(manifest)
    sources = [(stage / 'media' / label, dest) for label, dest in roots.items()]
    kept = []
    for i, dest in enumerate(files):
        source = stage / 'settings' / 'settings' / f'{i:04d}'
        if dest.is_file() and sha256(source) == sha256(dest):
            continue
        if not os.access(dest.parent, os.W_OK):
            kept.append(dest.name)
            continue
        sources.append((source, dest))
    prepared = []
    try:
        for source, dest in sources:
            new = _plain_path(dest.parent / f'.{dest.name}.restore-{stamp}')
            old = _plain_path(dest.parent / f'.{dest.name}.before-{stamp}')
            if new.exists() or old.exists():
                raise BackupError('A previous restore file needs operator review.')
            prepared.append((dest, new, old))
            try:
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, new)
                else:
                    with source.open('rb') as src, new.open('xb') as output:
                        os.chmod(new, 0o600)
                        shutil.copyfileobj(src, output)
            except PermissionError as exc:
                raise BackupError(f'The app cannot write next to {dest.name}; give it write access to that folder first.') from exc
        return prepared, kept
    except Exception:
        _clean_prepared(prepared)
        raise


def _remove(path):
    _plain_path(path)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _clean_prepared(prepared):
    for _, new, _ in prepared:
        _remove(new)


def _install_files(prepared, installed):
    for dest, new, old in prepared:
        for path in (dest, new, old):
            _plain_path(path)
        existed = dest.exists()
        if existed:
            dest.rename(old)
        installed.append((dest, old, existed))
        new.rename(dest)


def _rollback_files(installed):
    for dest, old, existed in reversed(installed):
        _remove(dest)
        if existed:
            old.rename(dest)


def _pg_admin(config):
    import psycopg
    kwargs = {k: config[v] for k, v in [('user', 'USER'), ('password', 'PASSWORD'),
                                      ('host', 'HOST'), ('port', 'PORT')] if config.get(v)}
    return psycopg.connect(dbname=getattr(settings, 'BACKUP_MAINTENANCE_DATABASE', 'postgres'), **kwargs)


def _pg_rename(config, first, second, third):
    """Both renames commit together; PostgreSQL rolls both back if either fails."""
    from psycopg import Error, sql
    try:
        admin = _pg_admin(config)
    except Error as exc:
        raise BackupError('Could not connect to PostgreSQL to rename databases; nothing was changed.') from exc
    try:
        try:
            with admin.cursor() as cursor:
                cursor.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                               'WHERE datname IN (%s, %s) AND usename = current_user AND pid <> pg_backend_pid()',
                               [first, second])
                cursor.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(first), sql.Identifier(third)))
                cursor.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(second), sql.Identifier(first)))
                cursor.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS false').format(sql.Identifier(third)))
                cursor.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(first)))
        except Error as exc:
            # Refused before COMMIT (e.g. another role is connected): the transaction never committed.
            raise BackupError('PostgreSQL refused the rename (another session may be connected); nothing was changed.') from exc
        try:
            admin.commit()
        except Error as exc:
            # A lost connection at COMMIT has an uncertain outcome. Never reopen the app.
            raise RecoveryRequired('PostgreSQL connection failed during rename; inspect the database names before recovery.') from exc
    finally:
        try:
            admin.close()
        except Error:
            pass


def _sqlite_live(config):
    name = str(config['NAME'])
    if name == ':memory:' or name.startswith('file:'):
        raise BackupError('Switch-over requires a file-backed SQLite database.')
    live = _configured(name)
    if not live.is_file() or live.is_relative_to(backup_root()):
        raise BackupError('The live SQLite database must be a file outside BACKUP_DIR.')
    return live


def _swap_database(config, copy_config, safety_stamp):
    live_connection().close()
    connections.close_all()
    if _engine(config) == 'postgresql':
        old = _pg_name(config, 'before', safety_stamp)
        _pg_rename(config, str(config['NAME']), str(copy_config['NAME']), old)
        return old
    live = _sqlite_live(config)
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(live) + suffix).exists():
            raise BackupError('SQLite sidecars remain; stop external writers before switching.')
    new = _plain_path(live.parent / f'.{live.name}.restore-{safety_stamp}')
    old = _plain_path(live.parent / f'.{live.name}.before-{safety_stamp}')
    if new.exists() or old.exists():
        raise BackupError('A previous SQLite recovery file already exists.')
    try:
        with Path(copy_config['NAME']).open('rb') as source, new.open('xb') as output:
            os.chmod(new, 0o600)
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        live.rename(old)
    except Exception:
        _remove(new)
        raise
    try:
        new.rename(live)
    except Exception:
        try:
            old.rename(live)
        except Exception as rollback_exc:
            raise RecoveryRequired('SQLite promotion failed and the original database could not be put back.') from rollback_exc
        _remove(new)
        raise
    return str(old)


def _rollback_database(config, copy_config, old):
    live_connection().close()
    connections.close_all()
    if _engine(config) == 'postgresql':
        _pg_rename(config, str(config['NAME']), old, str(copy_config['NAME']))
    else:
        # The original staged restore copy remains available for diagnosis.
        live = _sqlite_live(config)
        os.replace(_plain_path(old), live)


def _catalog():
    return [(model, list(model.objects.values())) for model in (Backup, BackupSchedule, RestoreLog)]


def _write_catalog(catalog):
    for model, rows in catalog:
        for original in rows:
            row = original.copy()
            pk = row.pop(model._meta.pk.attname)
            model.objects.update_or_create(pk=pk, defaults=row)
            if 'created_at' in row:
                model.objects.filter(pk=pk).update(created_at=row['created_at'])


def _checkpoint(step):
    """Fault-injection seam; all production steps remain synchronous."""


def switch_over(backup, confirmed=False):
    if not confirmed:
        raise BackupError('Switch-over requires explicit RESTORE confirmation (CLI: --yes).')
    with file_lock():
        if control_path('MAINTENANCE').exists():
            raise BackupError('Maintenance is already active; review the previous restore journal.')
        manifest = verify_backup(backup)
        stage, copy_config = _copy_report(backup, manifest)
        config = copy.deepcopy(live_connection().settings_dict)
        if _engine(config) == 'sqlite':
            _sqlite_live(config)
        else:
            _pg_name(config, 'before', backup.stamp)
        report = {'stamp': backup.stamp, 'status': 'failed', 'step': 'enter maintenance'}
        prepared, installed, old, catalog = [], [], None, None
        flag = control_path('MAINTENANCE')
        with flag.open('x') as output:
            os.chmod(flag, 0o600)
            output.write(backup.stamp + '\n')
        # The middleware holds a shared lock for each non-backup request. Drain it.
        with file_lock('.requests', blocking=True):
            log = None
            try:
                log = RestoreLog.objects.create(backup=backup, action='switch', status='running', step='safety backup')
                report['step'] = 'safety backup'
                started = timezone.now()
                safety = _create_backup('safety')
                if (safety.reason != 'safety' or safety.status != 'verified' or safety.stamp == backup.stamp
                        or safety.created_at < started):
                    raise BackupError('A fresh verified safety backup is required.')
                verify_backup(safety)
                report['safety_stamp'] = safety.stamp
                log.safety_stamp = safety.stamp
                log.save(update_fields=['safety_stamp'])
                report['step'] = 'after safety backup'
                _checkpoint('after_safety')
                report['step'] = 'prepare live files'
                prepared, kept = _prepare_files(stage, manifest, safety.stamp)
                if kept:
                    report['settings_kept'] = kept
                report['file_plan'] = [{'destination': str(dest), 'new': str(new), 'previous': str(previous),
                                        'existed': dest.exists()} for dest, new, previous in prepared]
                catalog = _catalog()
                report['step'] = 'database swap'
                log.step = report['step']
                log.save(update_fields=['step'])
                report['previous_database'] = (_pg_name(config, 'before', safety.stamp) if _engine(config) == 'postgresql'
                                               else str(_sqlite_live(config).parent / f'.{_sqlite_live(config).name}.before-{safety.stamp}'))
                _json(stage / 'SWITCH.json', report)
                old = _swap_database(config, copy_config, safety.stamp)
                report['previous_database'] = old
                report['step'] = 'after database swap'
                _json(stage / 'SWITCH.json', report)
                _checkpoint('after_database')
                report['step'] = 'install attachments and settings'
                _json(stage / 'SWITCH.json', report)
                _install_files(prepared, installed)
                _checkpoint('after_files')
                report['step'] = 'record restored catalog'
                _write_catalog(catalog)
                report.update(status='complete', step='complete', restart_required=True)
                backup.restore_report = report
                backup.save()
                log.status, log.step, log.report = 'complete', 'complete', report
                log.save()
                _json(stage / 'SWITCH.json', report)
            except Exception as exc:
                report['status'] = 'recovery_required' if isinstance(exc, RecoveryRequired) else 'failed'
                report['error'] = f'Stopped at {report["step"]}: {_safe_error(exc)}'
                try:
                    _rollback_files(installed)
                    if old is not None:
                        _rollback_database(config, copy_config, old)
                        report['rolled_back'] = True
                except Exception as rollback_exc:
                    report.update(status='recovery_required', rollback_error=_safe_error(rollback_exc))
                if report['status'] != 'recovery_required':
                    try:
                        backup.restore_report = report
                        backup.save()
                        if log:
                            log.status, log.step, log.report, log.error = report['status'], report['step'], report, report['error']
                            log.save()
                    except Exception as record_exc:
                        report.update(status='recovery_required', record_error=_safe_error(record_exc))
            finally:
                try:
                    _json(stage / 'SWITCH.json', report)
                    if report['status'] != 'recovery_required':
                        _clean_prepared(prepared)
                        control_path('GENERATION').write_text(f'{backup.stamp}:{timezone.now().isoformat()}')
                        from .config import clear_cache
                        clear_cache()
                        flag.unlink()
                except Exception as final_exc:
                    report.update(status='recovery_required', final_error=_safe_error(final_exc))
                    report.setdefault('error', f'Stopped at finalizing restore: {_safe_error(final_exc)}')
                    try:
                        _json(stage / 'SWITCH.json', report)
                    except (OSError, BackupError):
                        pass  # Keep maintenance active if the journal disk is unavailable.
        if report['status'] != 'complete':
            raise BackupError(report['error'] + (' Rollback needs operator recovery; maintenance remains active.'
                                               if report['status'] == 'recovery_required' else ' Live data is unchanged.'))
        return report


def prune_scheduled(retention):
    # Caller holds the operation lock. Never remove referenced restore/safety history.
    # Only verified backups count toward N; failed scheduled ones older than the Nth go too.
    scheduled = Backup.objects.filter(reason='scheduled')
    kept = list(scheduled.filter(status='verified').order_by('-stamp').values_list('stamp', flat=True)[:retention])
    if len(kept) < retention:
        return
    for backup in scheduled.filter(stamp__lt=kept[-1]).order_by('stamp'):
        if backup.restore_logs.exists() or backup.restore_report:
            continue
        path = checked_path(backup.stamp)
        if Path(backup.path) != path:
            raise BackupError('Refusing retention for an invalid backup path.')
        if path.exists():
            shutil.rmtree(path)
        backup.delete()


def get_schedule():
    # Native installs ship a timer, so their site settings turn nightly backups on for a new shop.
    row, _ = BackupSchedule.objects.get_or_create(
        pk=1, defaults={'enabled': getattr(settings, 'BACKUP_SCHEDULE_ENABLED_DEFAULT', False)})
    return row


def schedule_timezone():
    # WSGI applies Business.time_zone at startup; management commands do not.
    # Read it explicitly so the timer and page agree, independent of process cache.
    name = Business.objects.values_list('time_zone', flat=True).first()
    try:
        return ZoneInfo(name) if name else timezone.get_default_timezone()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BackupError('The saved shop time zone is invalid; correct Business Settings first.') from exc


def next_run(schedule, now=None):
    if not schedule.enabled:
        return None
    zone = schedule_timezone()
    now = timezone.localtime(now or timezone.now(), zone)
    day = now.date()
    due = timezone.make_aware(datetime.combine(day, schedule.time), zone)
    if schedule.last_attempt_day == day or now >= due:
        if schedule.last_attempt_day == day:
            due += timedelta(days=1)
    return due


def run_scheduled_backup(now=None):
    with file_lock():
        ensure_available()
        now = timezone.localtime(now or timezone.now(), schedule_timezone())
        schedule = get_schedule()
        if not schedule.enabled or now.time().replace(tzinfo=None) < schedule.time or schedule.last_attempt_day == now.date():
            return None
        # Record attempt before work: repeated timer invocations never duplicate a day.
        schedule.last_attempt_day = now.date()
        schedule.last_run, schedule.last_error = now, ''
        schedule.save()
        try:
            backup = _create_backup('scheduled')
            if backup.status != 'verified':
                raise BackupError('Scheduled backup did not verify; retention skipped.')
            prune_scheduled(schedule.retention)
            return backup
        except Exception as exc:
            schedule.last_error = _safe_error(exc)
            schedule.save(update_fields=['last_error'])
            raise
