# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.backup_commands import BackupCommand


class Command(BackupCommand):
    help = 'Take a fresh safety backup, then switch to an already-verified restore copy.'
    action = 'switch_over'
