# SPDX-License-Identifier: AGPL-3.0-or-later
import json

from django.core.management.base import BaseCommand, CommandError

from . import backups


class BackupCommand(BaseCommand):
    action = ''

    def add_arguments(self, parser):
        if self.action == 'backup':
            parser.add_argument('--reason', choices=['manual', 'scheduled', 'safety'], default='manual')
            parser.add_argument('--keep', type=int, help='After it verifies, keep only this many scheduled backups.')
        if self.action in ('restore_copy', 'switch_over'):
            parser.add_argument('stamp')
        if self.action == 'restore_copy':
            parser.add_argument('--discard', action='store_true',
                                help='Remove this stamp\'s restore copy instead, so it can be restored again.')
        if self.action == 'switch_over':
            parser.add_argument('--yes', action='store_true', help='Confirm switching the live shop to the verified copy.')

    def handle(self, *args, **options):
        try:
            if self.action == 'backup':
                row = backups.create_backup(options['reason'], keep=options['keep'])
                result = {'stamp': row.stamp, 'status': row.status, 'size': row.size}
            elif self.action == 'list_backups':
                with backups.file_lock():
                    result = list(backups.list_backups().values('stamp', 'reason', 'status', 'size', 'error', 'download_sha256'))
            elif self.action == 'run_scheduled_backup':
                row = backups.run_scheduled_backup()
                result = {'stamp': row.stamp, 'status': row.status} if row else {'status': 'not due'}
            else:
                backups.checked_path(options['stamp'])
                with backups.file_lock():
                    backups.ensure_available()
                    row = backups.find_backup(options['stamp'])
                if options.get('discard'):
                    backups.discard_copy(row)
                    result = {'stamp': row.stamp, 'status': 'discarded'}
                elif self.action == 'restore_copy':
                    result = backups.restore_to_copy(row)
                else:
                    result = backups.switch_over(row, confirmed=options['yes'])
                if result['status'] == 'failed':
                    raise backups.BackupError(f'Stopped at {result["step"]}: {result["error"]}')
            self.stdout.write(json.dumps(result, indent=2, default=str))
        except backups.BackupError as exc:
            raise CommandError(str(exc)) from exc
