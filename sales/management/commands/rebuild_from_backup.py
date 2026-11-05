# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuild business data from a clean QB POS dataset (current.sqlite). Validation-only unless --commit.

History cutoff: `--history-from 2021-09-01` loads sales documents from that day on only (all customers and
items; held receipts and open POs regardless of date; balances carried only as far as charges in the window explain
them). `--history-years 5` computes the date as the rule says — five years back from the cutover day (today, or
`--cutover-date`), rounded down to a sales-tax quarter start. An explicit --history-from always wins. Without either
the load is the full history.

Balance cutoff: `--balances-from 2023-09-01` (or `--balance-years 3`, same rounding) forgives
what is still due on Account charges dated before that day (payments applied FIFO, oldest first) and carries the
due on newer charges; it must be on/after the history cutoff. Without it the history date drives both, as before.
`--credits history|all|balances` says which credit balances (money owed TO the customer) carry: `history` keeps those
of customers with a completed receipt since the history cutoff, `all` keeps every credit, `balances` ages the credit
itself (only credits put on the account since the balance cutoff carry, oldest spent first). It is required with a balance cutoff (no silent
default); without one, `history` applies as before.
"""
import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from sales.rebuild import CREDIT_RULES, RebuildError, history_cutoff, rebuild_from_backup


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f'{value!r} is not a date (YYYY-MM-DD)') from exc


class Command(BaseCommand):
    help = 'Wipe business data (not printers, users or sessions) and load current.sqlite. Dry run unless --commit.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to current.sqlite')
        parser.add_argument('--continue-qb-numbers', action='store_true', help='Cutover: continue each series after its highest QB number.')
        parser.add_argument('--commit', action='store_true', help='Apply the load; otherwise validate, load and roll back.')
        parser.add_argument('--discard-test-data', action='store_true', help='Allow dropping completed documents that were created in this app rather than imported.')
        parser.add_argument('--history-from', metavar='YYYY-MM-DD', help='History cutoff: load sales documents dated on/after this day only (wins over --history-years).')
        parser.add_argument('--history-years', type=int, metavar='N', help='History cutoff by rule: N years back from the cutover day, rounded down to a sales-tax quarter start.')
        parser.add_argument('--balances-from', metavar='YYYY-MM-DD', help='Balance cutoff: forgive what is still due on Account charges dated before this day (wins over --balance-years; default: the history cutoff).')
        parser.add_argument('--balance-years', type=int, metavar='N', help='Balance cutoff by rule: N years back from the cutover day, rounded down to a sales-tax quarter start.')
        parser.add_argument('--credits', choices=CREDIT_RULES, default=None, help='Which credit balances carry: history (customers with a receipt since the history cutoff), all, or balances (the credit itself aged from the balance cutoff, oldest spent first). Required with a balance cutoff.')
        parser.add_argument('--cutover-date', metavar='YYYY-MM-DD', help='The cutover day --history-years / --balance-years count back from (default: today).')

    def cutoff(self, options, explicit, years, cutover):
        """A cutoff date from its explicit option or its N-years rule (None without either)."""
        flag = lambda name: '--' + name.replace('_', '-')
        if options[explicit]:
            value = iso_date(options[explicit])
            if options[years] is not None:
                self.stderr.write(f'{flag(explicit)} {value} given: {flag(years)} ignored')
            return value
        if options[years] is not None:
            if options[years] < 1:   # a supplied 0 is refused, not treated as "not given" (a prior review, finding 2)
                raise CommandError(f'{flag(years)} must be at least 1')
            value = history_cutoff(options[years], cutover)
            self.stderr.write(f"{flag(years)} {options[years]} from {cutover or 'today'}: {explicit.split('_')[0]} from {value}")
            return value
        return None

    def handle(self, *args, **options):
        cutover = iso_date(options['cutover_date']) if options['cutover_date'] else None
        if cutover and not (options['history_years'] or options['balance_years']):
            raise CommandError('--cutover-date only makes sense with --history-years or --balance-years')
        history_from = self.cutoff(options, 'history_from', 'history_years', cutover)
        balances_from = self.cutoff(options, 'balances_from', 'balance_years', cutover)
        if balances_from and history_from and balances_from < history_from:
            raise CommandError(f'--balances-from {balances_from} is earlier than the history cutoff {history_from}: a carried balance must come from loaded receipts')
        if options['credits'] and options['credits'] != 'history' and not (history_from or balances_from):
            raise CommandError('--credits only applies with a history or balance cutoff')
        if balances_from and not options['credits']:
            # A separate balance cutoff must say what happens to store credits: `history` keeps credits by receipt
            # activity in the history window, an in-between answer nobody chose (credits follow the 3-year
            # rule, --credits balances). No silent default.
            raise CommandError('--balances-from / --balance-years need an explicit --credits (history, all or balances)')
        credits = options['credits'] or 'history'
        try:
            result = rebuild_from_backup(options['path'], commit=options['commit'], discard_test_data=options['discard_test_data'],
                                         progress=lambda message: self.stderr.write(message), continue_qb_numbers=options['continue_qb_numbers'],
                                         history_from=history_from, balances_from=balances_from, credits=credits)
        except RebuildError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=1, sort_keys=True, default=str))
