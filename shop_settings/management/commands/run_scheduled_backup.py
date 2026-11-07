# SPDX-License-Identifier: AGPL-3.0-or-later
from shop_settings.backup_commands import BackupCommand


class Command(BackupCommand):
    help = 'Run the nightly backup if enabled and due; at most one attempt per local day.'
    action = 'run_scheduled_backup'
