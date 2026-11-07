# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.backup_commands import BackupCommand


class Command(BackupCommand):
    help = 'Create and verify a database, attachments and settings backup.'
    action = 'backup'
