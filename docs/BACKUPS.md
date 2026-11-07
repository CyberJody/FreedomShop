<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Backups and two-step restore

Administrators open **File → Backups** or **Settings → Backups** at
`/settings/backups/`. **Back up now** writes a manual backup and verifies it.
The table shows creation date, reason, bytes, verification status, a download and
its SHA-256. Downloads are streamed from a temporary file, using bounded memory;
the HTTP `X-Checksum-SHA256` and `ETag` contain the same digest shown on the page.
Failed verification disables download and restore. Downloads include settings
and private shop records: store them with the same protection as the live data.
The listing, command output and manifest never show settings file contents.

## Configuration and format

`BACKUP_DIR` defaults to `DATA_DIR / "backups"` (or `BASE_DIR / "data/backups"`
when DATA_DIR is unset), worked out at run time, so a site settings module that
changes `DATA_DIR` moves it too. Keep this directory private and writable by the app
account, on persistent storage outside the release tree. All workers and commands
must use the same directory and local filesystem locking. Back up this directory
to another disk as part of the operator's recovery procedure; local backups alone
do not protect against loss of the disk.

Each backup is exactly:

```text
backups/YYYYMMDD-HHMMSS/
    db.dump
    media.tar.gz
    settings.tar.gz
    MANIFEST.json
    SHA256SUMS
```

`db.dump` is PostgreSQL custom format (`pg_dump -Fc`) or a self-contained SQLite
snapshot created using the SQLite online backup API. The two formats are not
interchangeable. `media.tar.gz` includes `MEDIA_ROOT`, `DATA_DIR/uploads` (override
with `BACKUP_UPLOAD_ROOT`), and `TRACKER_UPLOAD_DIR` if it is outside those roots.
Archives store neutral labels `media`, `uploads`, `tracker`, never absolute paths.
Missing attachment roots become empty directories. Symbolic links, hard-linked tar
entries, special files, overlapping live destinations and archive traversal paths
are refused. Configured paths (BACKUP_DIR, attachment roots, settings files, the
SQLite file) are resolved to their real location first, so they may sit on a
linked disk; nothing below them may be a symbolic link. Put the
SQLite live database outside BACKUP_DIR and outside the attachment roots.

Set `BACKUP_SETTINGS_FILES` to an explicit ordered list of settings files, for
example the external site settings module and the separate secret file. With no
explicit list, only the active `DJANGO_SETTINGS_MODULE` file is included, and only
when it is outside the repository. In-repository settings, imported modules,
environment variables and separate secret files are **not discovered**. List any
required external files explicitly. An explicit missing file fails the backup.
Settings files use numbered archive members; the manifest contains basenames only.
Restore maps these members to the currently configured allowlist, never to a path
supplied in an archive. The labels, file order and basenames must match. The native
settings template lists `settings_site.py` and the secret key file.

The versioned, host-free manifest records `APP_VERSION` (or packaging's
`freedomshop.__version__`, falling back to `development` in source-only checkouts),
database engine, applied migrations, reason, stamp, attachment labels, settings
basenames and payload sizes. SHA256SUMS covers all four other files (the checksum
file cannot hash itself). Verification rehashes each payload, validates the exact
file set and sizes, lists both tars with safe-member validation, then runs
`pg_restore --list` or SQLite `integrity_check`. It does not execute settings code.
SHA-256 detects damage; it does not authenticate an archive from an untrusted
source. Backups are not encrypted. Keep filesystem access restricted.

The database catalog stores `Backup`, `BackupSchedule`, and `RestoreLog`. The
backup directory is addressed by a validated stamp, not by a request-supplied path
or the catalog path field. Concurrent backup/restore commands refuse the operation
lock. Stamps use UTC and one-second resolution; a same-second collision advances
to the next unused second. The table's creation time remains the actual time.

## Restore, then switch

1. Choose **Restore into a copy…**. Live data stays unchanged. PostgreSQL gets a
   newly created database `<live>_restore_<stamp>`; SQLite gets a new file under
   `BACKUP_DIR/.restore/<stamp>/`. Attachments and settings are unpacked into that
   private staging directory. Existing copies are never reused or overwritten.
2. Review the report: a table count, exactly one business row, consistent applied
   migrations and no pending migrations are required. SQLite additionally checks
   database integrity and foreign keys. A backup from a different application
   schema requires matching application code; the restore never silently migrates
   it. Copy files and source checksums are bound to the report and rechecked at
   switch-over. This is a structural sanity check, not a replay of every sale.
3. Type **RESTORE** and press **Switch over**. The app writes
   `BACKUP_DIR/MAINTENANCE`. The first middleware returns HTTP 503 before auth or
   database reads. A separate request lock drains active normal requests. A fresh
   `safety` backup of the live database, attachments and settings must verify
   before any replacement happens. Safety backups are never automatically deleted.
4. New live files are prepared next to their configured destinations so file
   renames stay on the same filesystem. Settings files equal to the backup are left
   alone. A changed settings file whose folder the app cannot write (natively
   `/etc/freedomshop`) is kept as it is and listed in the report as
   `settings_kept`; its backup copy stays in the staging folder. The database switches first. PostgreSQL
   uses a maintenance connection and commits both database renames in one
   transaction; SQLite closes connections and renames files. Only after this
   succeeds do attachment/settings renames occur. Catalog/history rows from the
   former live database are carried forward into the restored database.
5. On success the maintenance flag is removed. A generation marker makes app
   workers drop old connections and cached business/tax settings on their next
   request. **Restart all workers to load any restored Python settings**; this
   page cannot reload process configuration. Sessions/users follow the restored
   database, so the current login may end. The prior database and adjacent
   `.before-<safety stamp>` file/directory copies remain for operator recovery.

Stop external database writers, jobs and consumers before switching. The request
lock coordinates this app's synchronous request handlers; it cannot drain an
unrelated process, an arbitrary management command or a streaming-response body.
The application must not perform writes from streaming responses. All app workers
must run the maintenance middleware first. Do not use a network filesystem with
unreliable flock semantics. SQLite switch-over refuses in-memory/URI databases
and databases with remaining WAL/journal/shared-memory sidecars.

Ordinary exceptions trigger file rollback and then database rollback. A failure
before the database swap leaves the live database and files untouched; a failure
after the swap returns them to their previous state when rollback succeeds. The
report names the stopped step. A rollback failure leaves maintenance active and
reports `recovery_required`: never remove the flag without inspecting the state.
When database identity is uncertain, the core stops writing database audit rows;
the filesystem journal is authoritative until the operator completes recovery.
Other core backup operations and commands refuse maintenance mode before opening
the database. The error response also avoids database-dependent template context.
A power failure or process kill cannot be made atomic across a database and several
filesystems. Inspect `.restore/<stamp>/SWITCH.json`, `REPORT.json`, the safety
backup, prior database and adjacent `.before-*` copies before manual recovery.
Do not delete these artifacts while recovery is under review. A failed copy keeps
its partial artifacts for diagnosis. **Discard restore copy** on the report page
(`restore_copy STAMP --discard`) drops the `<live>_restore_<stamp>` database and the
staging folder so the stamp can be restored again; it is refused while a switch-over
needs recovery. The prior database and `.before-*` copies are never deleted
automatically.

The web switch-over runs inside one app worker request. A safety backup that takes
longer than the worker timeout (gunicorn `--timeout 300` natively) gets the worker
killed and leaves maintenance on, so for large shops run `sudo freedomshop restore`
or `switch_over` from the shell instead.

A backup folder with no catalog row (a fresh or replacement install, or a folder
copied in from another disk) is adopted by `restore_copy STAMP`: it is verified and
recorded with its manifest's reason, then restored as usual.

## PostgreSQL role and tools

Install compatible PostgreSQL client tools (`pg_dump`, `createdb`, `pg_restore`,
`dropdb`) and the app's psycopg driver; the native installer and the container image
include them. The application role needs **CREATEDB**, ownership of
the live and restore databases, read access to all dumped objects, and CONNECT to
the maintenance database (default `postgres`; override
`BACKUP_MAINTENANCE_DATABASE`). Use the same role for application connections so it
can terminate its own sessions during rename. Other roles' active sessions cause
a safe rename failure (nothing changes and maintenance is lifted); stop them first.
Only a connection lost at COMMIT is treated as uncertain. The role does **not**
need superuser. The native installer creates the `freedomshop` role with CREATEDB
and grants it again on re-run; Compose's database owner already has it.
The live database name must leave room for the restore/safety suffix under
PostgreSQL's 63-byte identifier limit (39 bytes maximum for the live name).
Database identifiers are quoted with psycopg; executable calls use argv arrays,
with passwords passed in the subprocess environment, never command arguments.
Use ordinary Django NAME/USER/PASSWORD/HOST/PORT settings; custom libpq service or
OPTIONS-only connection profiles are not supported by the backup subprocesses.

The app role also needs write permission to the folders that hold the live
media/upload directories (natively `/var/lib/freedomshop`, which the installer gives
to `freedomshop` and `freedomshop.service` makes writable). Do not grant broad write
access to the release tree. Root-owned site settings are kept as described above;
an operator copies a changed one from the staging folder if it is wanted. File preparation refuses existing
`.restore-*`/`.before-*` names so recovery copies cannot be overwritten.

## Commands and scheduling

```sh
python manage.py backup                         # manual
python manage.py backup --keep 14               # then keep the newest 14 verified scheduled backups
python manage.py backup --reason safety
python manage.py list_backups
python manage.py restore_copy 20261105-020000
python manage.py restore_copy 20261105-020000 --discard
python manage.py switch_over 20261105-020000 --yes
python manage.py run_scheduled_backup
```

Commands output JSON and exit nonzero on a failure. `--yes` is the CLI equivalent
of the typed confirmation; it still requires a previously verified restore copy
and a fresh safety backup. `backup --reason scheduled` creates that reason only;
use `run_scheduled_backup` for due checks, daily idempotency and retention.

Nightly backups start off, unless the site settings set
`BACKUP_SCHEDULE_ENABLED_DEFAULT = True` (the native template does, because it
installs the timer); this only seeds a new shop's schedule. Turn them on or off,
choose a local time and a retention count (1–3650) on the page. The saved Business time zone applies, with
Django's TIME_ZONE as the fallback. Both the web page and timer read that choice
directly, so the command does not depend on WSGI startup.
`run_scheduled_backup` takes the operation lock, checks enabled/time/day, and
records the local day's attempt before work. Repeated invocations on that day do
nothing, including after a failure. Errors and last/next run appear on the page;
make a manual backup if a failed day's backup needs retry. After a newly verified
scheduled backup, only the newest N *verified* scheduled backups are kept; older
scheduled backups, failed ones included, are removed. Failed ones newer than the
Nth verified backup stay until a later run. Manual, safety
and backups referenced by restore history are kept, so history can exceed N.
A failed new backup never triggers retention.

`deploy/freedomshop-backup.timer` and `.service`, installed by the native
installer, run `freedomshop manage run_scheduled_backup` every five minutes, so the
in-app time stays authoritative. The timer must run on every day; DST may repeat or
skip wall-clock times, but the daily attempt guard prevents duplicates and a late
invocation runs that day's due backup. Other deployments run the same command
from their own scheduler.

## Native shell commands

`sudo freedomshop backup [--keep N]` runs `manage.py backup [--keep N]` as the
`freedomshop` user and prints the new folder. `sudo freedomshop restore FOLDER`
takes a `YYYYMMDD-HHMMSS` folder, copies it into BACKUP_DIR if it is elsewhere, runs
`restore_copy` and then `switch_over --yes`, and restarts the service so every worker
loads the restored data. `--wheelhouse` is accepted but not needed: no code is
installed. `freedomshop upgrade` keeps its own full-installation rollback snapshot
(`backups/before-VERSION-…`, with the release code), because this restore refuses
a backup whose migrations differ from the running code.
