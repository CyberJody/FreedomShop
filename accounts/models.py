# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import models
from django.utils import timezone

class Cashier(models.Model):
    """PIN identity only, deliberately not an account that can sign in."""
    name = models.CharField(max_length=100, unique=True)
    pin_hash = models.CharField(max_length=128)
    active = models.BooleanField(default=True)


class LoginWindow(models.Model):
    key=models.CharField(max_length=64,unique=True)
    failures=models.PositiveIntegerField(default=0)
    started_at=models.DateTimeField(default=timezone.now)
