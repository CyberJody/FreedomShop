# SPDX-License-Identifier: AGPL-3.0-or-later
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from sales.services import WorkflowError
from shop_settings.demo import CASHIERS, DemoLoader, reset_demo
from shop_settings.setup import DemoRefused


class Command(BaseCommand):
    help = 'Load generated Demo Home & Hardware data into an empty test shop. Creates no logins or PINs.'

    def add_arguments(self, parser):
        parser.add_argument('--with-history', action='store_true', help='Include sales and purchasing history.')
        parser.add_argument('--days', type=int, default=120, help='Calendar-day window; history volume scales with days (default 120).')
        parser.add_argument('--seed', type=int, default=7)
        parser.add_argument('--force', action='store_true', help='Append a separate demo batch despite an existing catalog.')
        parser.add_argument('--reset-demo', action='store_true', help='Delete provably demo-owned rows before loading; refuse untaggable or mixed data.')
        parser.add_argument('--preserve-settings', action='store_true', help='Keep wizard business and tax settings.')

    def handle(self, *args, **options):
        try:
            # Reset and reload succeed or roll back together.
            with transaction.atomic():
                if options['reset_demo']:
                    reset_demo()
                loader = DemoLoader(days=options['days'], seed=options['seed'], with_history=options['with_history'],
                                    force=options['force'], preserve_settings=options['preserve_settings'],
                                    progress=self.stdout.write if options['verbosity'] else None)
                counts = loader.run()
        except (DemoRefused, WorkflowError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS('Demo loaded: ' + ', '.join(f'{value} {key.replace("_", " ")}' for key, value in counts.items()) + '.'))
        self.stdout.write('Cashier stamps: ' + ', '.join(CASHIERS) + '. No logins or PINs created; see docs/DEMO.md.')
