# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local certificate liability. Never infer an opening balance from QB tender history."""
from collections import defaultdict
from decimal import Decimal
from django.db import IntegrityError, transaction
from .models import GiftCertificate, GiftRedemption
from .services import WorkflowError


def apply(document, lines, payments, *, active):
    """Inside save_document's transaction: reverse this receipt's old effect and apply its new effect.

    Lock all existing affected certificates by number, so concurrent redemptions
    cannot overspend. New numbers are protected by a unique constraint. Issued
    certificates cannot be changed/cancelled after anyone has redeemed them.
    """
    issued = {}
    wanted = defaultdict(Decimal)
    if active:
        for line in lines:
            if line.certificate_number:
                if line.certificate_number in issued:
                    raise WorkflowError('Use a different number for each gift certificate.')
                issued[line.certificate_number] = line.price
        for payment in payments:
            if payment['method'] == 'gift':
                number = payment.get('reference', '')
                if not number:
                    raise WorkflowError('Enter the gift certificate number.')
                if Decimal(payment['amount']) <= 0:
                    raise WorkflowError('Gift certificate redemptions must be greater than zero.')
                wanted[number] += Decimal(payment['amount'])
    old_redemptions = dict(document.gift_redemptions.values_list('certificate__number', 'amount'))
    old_issued = set(document.issued_certificates.values_list('number', flat=True))
    numbers = sorted(set(issued) | set(wanted) | set(old_redemptions) | old_issued)
    certs = {c.number: c for c in GiftCertificate.objects.select_for_update().filter(number__in=numbers).order_by('number')}
    for number in old_issued:
        cert = certs[number]
        if number not in issued or issued[number] != cert.face_value:
            if cert.redemptions.exclude(document=document).exists():
                raise WorkflowError(f'Gift certificate {number} has been redeemed; its sale cannot be changed or cancelled.')
    for number, value in issued.items():
        if number in certs and certs[number].issued_on_id != document.pk:
            raise WorkflowError(f'Gift certificate {number} already exists. Use a new number.')
        if number not in certs:
            try:
                with transaction.atomic():
                    certs[number] = GiftCertificate.objects.create(number=number, issued_on=document, face_value=value, balance=value)
            except IntegrityError as exc:
                raise WorkflowError(f'Gift certificate {number} already exists. Use a new number.') from exc
    for number in wanted:
        if number not in certs or number in old_issued - set(issued):
            raise WorkflowError(f'Unknown gift certificate {number}. Check the number; no balance has been established for it.')
        if number in issued:
            raise WorkflowError('A gift certificate cannot pay for its own issuing receipt.')
    document.gift_redemptions.all().delete()
    for number, cert in certs.items():
        balance = cert.balance + old_redemptions.get(number, Decimal('0'))
        if number in issued:
            balance += issued[number] - cert.face_value
            cert.face_value = issued[number]
        amount = wanted.get(number, Decimal('0'))
        if amount > balance:
            raise WorkflowError(f'Gift certificate {number} has ${balance:.2f} remaining; reduce its payment amount.')
        cert.balance = balance - amount
        cert.save(update_fields=['face_value', 'balance'])
        if amount:
            GiftRedemption.objects.create(certificate=cert, document=document, amount=amount)
    for number in old_issued - set(issued):
        certs[number].delete()
