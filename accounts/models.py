# SPDX-License-Identifier: AGPL-3.0-or-later
from django.conf import settings
from django.db import models
from django.utils import timezone

class Cashier(models.Model):
    """PIN identity only, deliberately not an account that can sign in."""
    name = models.CharField(max_length=100, unique=True)
    pin_hash = models.CharField(max_length=128)
    active = models.BooleanField(default=True)
    # Documents keep name strings. Retain aliases to recognize use after a rename.
    previous_names = models.JSONField(default=list, editable=False)


class AuditEntry(models.Model):
    """Account changes, committed with the change; never include credentials or hashes."""
    when = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    who = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                            related_name='account_audit_entries')
    actor_name = models.CharField(max_length=150)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=220)
    details = models.JSONField(default=dict)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-when', '-pk']


class AdminReauthenticationCache(models.Model):
    """Django DatabaseCache schema, migrated normally; contains only failure counters.

    Shared by application workers without requiring a cache-table management command.
    """
    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.TextField()
    expires = models.DateTimeField(db_index=True)


class LoginWindow(models.Model):
    key=models.CharField(max_length=64,unique=True)
    failures=models.PositiveIntegerField(default=0)
    started_at=models.DateTimeField(default=timezone.now)
