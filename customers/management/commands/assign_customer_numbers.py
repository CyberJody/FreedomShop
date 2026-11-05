# SPDX-License-Identifier: AGPL-3.0-or-later
"""backfill: give a Customer # to customers created in this app before numbering existed.

Dry run by default (nothing saved); --commit saves. Only app-made customers ('local:' source ids) with a blank number
are numbered, oldest first (creation order = primary key), continuing from the highest all-digit number on file.
Customers imported from QuickBooks with a blank number are listed and left alone.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from customers.models import Customer


class Command(BaseCommand):
    help = 'Assign customer numbers to app-made customers that have none (dry run unless --commit).'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Save the numbers (default: dry run, roll back).')

    def handle(self, *args, commit=False, **options):
        with transaction.atomic():
            Customer.lock_numbers()
            blank = Customer.objects.filter(customer_number='')
            todo = list(blank.filter(source_id__startswith=Customer.LOCAL_PREFIX).order_by('pk'))
            imported = list(blank.exclude(source_id__startswith=Customer.LOCAL_PREFIX).order_by('pk'))
            for customer in todo:
                customer.customer_number = Customer.next_customer_number()
                Customer.objects.filter(pk=customer.pk, customer_number='').update(customer_number=customer.customer_number)
                self.stdout.write(f'  {customer.pk}: {customer.display_name} -> {customer.customer_number}')
            self.stdout.write(f'app-made customers numbered: {len(todo)}')
            self.stdout.write(f'imported customers with no number (left as they are): {len(imported)}')
            for customer in imported:
                self.stdout.write(f'  {customer.pk}: {customer.display_name} ({customer.source_id})')
            if not commit:
                transaction.set_rollback(True)
                self.stdout.write('dry run: nothing saved (use --commit)')
            else:
                self.stdout.write('committed')
