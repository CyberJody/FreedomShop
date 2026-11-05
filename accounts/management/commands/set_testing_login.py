# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

class Command(BaseCommand):
    help='Create/update the shared testing login; password is read from stdin, never command arguments.'
    def add_arguments(self,parser): parser.add_argument('--username',default='staff')
    def handle(self,*args,**options):
        password=sys.stdin.readline().rstrip('\r\n')
        if not password: raise CommandError('Supply a password on standard input.')
        User=get_user_model()
        user,_=User.objects.get_or_create(username=options['username'])
        try: validate_password(password,user)
        except ValidationError as exc: raise CommandError('; '.join(exc.messages))
        user.set_password(password);user.is_active=True;user.is_staff=True;user.save()
        self.stdout.write(f'Testing login ready: {user.username}')
