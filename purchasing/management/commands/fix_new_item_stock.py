# SPDX-License-Identifier: AGPL-3.0-or-later
"""one-time repair: app-made items saved with a blank On-Hand Qty start at 0, as new items now do.

Before a new item saved with On-Hand left blank got an unknown ("Unverified") count, and receiving it was refused.
This sets On-Hand to 0 for items made in the app (no QB source) whose count is unknown and that have never moved stock
(no sale/return, receiving or stock adjustment). Average cost is left as it is; the first receiving sets it.
Imported QB items are never touched. Dry run unless --commit.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from purchasing.models import ReceivingMovement
from sales.models import InventoryAdjustment, Item, StockMovement


def candidates():
    moved = (Q(pk__in=StockMovement.objects.values('item_id')) | Q(pk__in=ReceivingMovement.objects.values('item_id'))
             | Q(pk__in=InventoryAdjustment.objects.filter(new_quantity__isnull=False).values('item_id')))
    return (Item.objects.filter(Q(source_id__isnull=True) | Q(source_id=''), on_hand__isnull=True)
            .exclude(moved).order_by('pk'))


class Command(BaseCommand):
    help = 'Set On-Hand Qty to 0 on app-made items saved with a blank count that never moved stock. Dry run unless --commit.'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Save the change (default: list only).')

    def handle(self, *args, **options):
        with transaction.atomic():
            items = list(candidates().select_for_update())
            for item in items:
                cost = 'blank' if item.average_cost is None else f'{item.average_cost:.2f}'
                self.stdout.write(f'  item {item.sku}  {item.display_name[:60]}  type={item.item_type}  '
                                  f'active={item.active}  avg cost={cost}  on hand: Unverified -> 0')
            skipped = (Item.objects.filter(Q(source_id__isnull=True) | Q(source_id=''), on_hand__isnull=True)
                       .count() - len(items))
            if options['commit']:
                for item in items:
                    item.on_hand = 0
                    item.version += 1
                    item.save(update_fields=['on_hand', 'version'])
            done = 'set to On-Hand 0.' if options['commit'] else 'would be set to On-Hand 0 (dry run; add --commit to save).'
            self.stdout.write(f'{len(items)} app-made item(s) {done} '
                              f'{skipped} app-made item(s) with an unknown count skipped because they already moved stock.')
