# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.backup_commands import BackupCommand


class Command(BackupCommand):
    help = 'List local backup metadata without settings contents.'
    action = 'list_backups'
