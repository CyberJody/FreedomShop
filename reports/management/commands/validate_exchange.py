# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from reports.exchange_validation import strict_json, validate_exchange


class Command(BaseCommand):
    help = 'Validate local exchange files against the final schema, checksum and checks (offline).'

    def add_arguments(self, parser):
        parser.add_argument('files', nargs='+', type=Path)

    def handle(self, *args, **options):
        failures = []
        for path in options['files']:
            try:
                validate_exchange(strict_json(path.read_bytes()))
                self.stdout.write(f'{path}: valid')
            except (ValueError, TypeError, OSError) as exc:
                failures.append(f'{path}: {exc}')
        if failures:
            raise CommandError('\n'.join(failures))
