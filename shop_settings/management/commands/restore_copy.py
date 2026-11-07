# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.backup_commands import BackupCommand


class Command(BackupCommand):
    help = 'Restore a backup into a new database and file copy; leave the live shop alone.'
    action = 'restore_copy'
