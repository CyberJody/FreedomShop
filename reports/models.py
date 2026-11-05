# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import models


class ExchangeRun(models.Model):
    """One durable delivery artifact per local calendar day; payload is immutable after export."""
    STATUSES = [(value, value.capitalize()) for value in ('pending', 'exported', 'acknowledged', 'failed')]
    day = models.DateField(unique=True)
    generated_at = models.DateTimeField(null=True, editable=False)
    file_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    status = models.CharField(max_length=16, choices=STATUSES, default='pending')
    ack_reference = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    # Keep the exact envelope, including generated_at, independently of outbox retention.
    payload = models.BinaryField(default=bytes, editable=False)
    exported_at = models.DateTimeField(null=True, editable=False)
    published_at = models.DateTimeField(null=True, editable=False)
    failure_stage = models.CharField(max_length=10, blank=True, editable=False)
    base_body = models.JSONField(default=dict, editable=False)
    push_responses = models.JSONField(default=list, editable=False)

    class Meta:
        ordering = ['-day']


class ExchangeCorrection(models.Model):
    """A historical ledger delta observed inside the cancelling transaction."""
    source_run = models.ForeignKey(ExchangeRun, on_delete=models.PROTECT, related_name='corrections')
    day = models.DateField(db_index=True)
    created_at = models.DateTimeField()
    source = models.CharField(max_length=200)
    before_body = models.JSONField()
    after_body = models.JSONField()
    commands = models.JSONField(default=list)
    effective_commands = models.JSONField(default=list)

    class Meta:
        ordering = ['created_at', 'pk']
