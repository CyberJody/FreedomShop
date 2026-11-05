# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from reports.exchange import days_between
from reports.exchange_sync import collect_acks, export_day, outbox_path, pending_days, push_run
from reports.models import ExchangeRun
from shop_settings.config import business


class Command(BaseCommand):
    help = 'Export closed days, collect FreedomBooks acknowledgements, or explicitly push retained commands.'

    def add_arguments(self, parser):
        choice = parser.add_mutually_exclusive_group()
        choice.add_argument('--day')
        choice.add_argument('--from', dest='start')
        choice.add_argument('--pending', action='store_true')
        parser.add_argument('--to', dest='end')
        parser.add_argument('--out', type=Path, help='Override Business.exchange_outbox')
        parser.add_argument('--collect-acks', action='store_true')
        parser.add_argument('--push', action='store_true', help='Opt-in draft HTTP push; requires endpoint')

    def handle(self, *args, **options):
        if bool(options['start']) != bool(options['end']):
            raise CommandError('Use --day alone, or both --from and --to.')
        selected = options['day'] or options['start'] or options['pending']
        if not selected and not options['collect_acks'] and not options['push']:
            raise CommandError('Choose --day, --from/--to, --pending, --collect-acks or --push.')
        if options['push'] and not business().exchange_endpoint:
            raise CommandError('Live push is disabled; set an exchange endpoint in Business Settings.')
        try:
            outbox = outbox_path(options['out']) if selected or options['collect_acks'] else None
            failures, runs = [], []
            if selected:
                days = pending_days() if options['pending'] else days_between(options['day'] or options['start'], options['day'] or options['end'])
                for day in days:
                    try:
                        run = export_day(day, outbox)
                        runs.append(run)
                        self.stdout.write(str(outbox / f'freedomshop-exchange-{day}.json'))
                        if run.status == 'failed':
                            failures.append(f'{day}: {run.error}')
                    except (ValueError, OSError) as exc:
                        failures.append(f'{day}: {exc}')
            if options['collect_acks']:
                failures.extend(collect_acks(outbox))
            if options['push']:
                if not selected:
                    runs = ExchangeRun.objects.filter(exported_at__isnull=False).exclude(status='acknowledged').order_by('day')
                for run in runs:
                    if push_run(run):
                        failures.append(f'{run.day}: live push failed; see the retained responses.')
                        break
            if failures:
                raise CommandError('\n'.join(failures))
        except (ValueError, TypeError, OSError) as exc:
            raise CommandError(str(exc)) from exc
