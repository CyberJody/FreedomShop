# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load the app's logins from a user file without disclosing any of its contents.

File shape (headings are matched loosely; the rest of each heading line is ignored):

    Admins:...
    <name> password <password>
    Shared register User
    register password <password>
    Sub Accounts,...
    <name> Pin <4 digits>
    (plain-language instruction lines are ignored)

- Admins become Django users in the ``Admin`` group (Admin Mode always on, cashier automatically).
- The register login becomes a Django user in the ``register`` group. An existing shared
  login's password is NEVER changed unless ``--set-register-password`` is given — without it the command only
  reports "same as file" / "differs from file". A missing register login is created from the file.
- PIN users are ``Cashier`` rows (never logins), PINs stored hashed.
- Anyone no longer in the file is DEACTIVATED, never deleted (admins: ``is_active=False``; PIN users: ``active=False``,
  hidden from the pop-up), so their names stay on past documents. Listed again later = reactivated.

Output is counts only. Nothing from the file is ever echoed, not even in error messages.
"""
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from accounts.models import Cashier
from accounts.roles import ADMIN_GROUP, REGISTER_GROUP, group

ENTRY = re.compile(r'^(?P<name>.+?)\s+(?P<kind>password|pin)\s+(?P<value>\S+)$', re.IGNORECASE)


def _section_for(line):
    head = line.casefold()
    if re.match(r'(sub[\s-]*accounts?|pin users?|sub[\s-]*users?)\b', head):
        return 'cashiers'
    if re.match(r'(shared\s+)?register\b', head):
        return 'register'
    if re.match(r'admins?\b', head):
        return 'admins'
    return None


@sensitive_variables()
def parse_users(path):
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        raise CommandError('Cannot read the user file.') from None
    admins, register, cashiers = {}, {}, {}
    section = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        entry = ENTRY.match(line)
        if entry is None:
            heading = _section_for(line)
            if heading is not None:
                section = heading
            elif len(line.split()) <= 4 and section is not None:
                # A few short words that are not "name password/pin value": most likely a typo in a credential line.
                raise CommandError(f'Invalid user file entry on line {number}.')
            continue  # instruction prose
        if section is None:
            raise CommandError(f'User file entry before any heading on line {number}.')
        name, kind, value = entry['name'].strip(), entry['kind'].casefold(), entry['value']
        target = {'admins': admins, 'register': register, 'cashiers': cashiers}[section]
        expected = 'pin' if section == 'cashiers' else 'password'
        if kind != expected or not name or len(name) > 100:
            raise CommandError(f'Invalid user file entry on line {number}.')
        if section == 'cashiers' and not re.fullmatch(r'[0-9]{4}', value):
            raise CommandError(f'PIN entries must contain four digits (line {number}).')
        # One person may be BOTH an admin login and a PIN user (signs in as an admin, and under
        # the register login switches in by PIN). Everything else stays refused: a name twice in one section, and any
        # overlap with the register login.
        clashes = [target, register] if section != 'register' else [admins, register, cashiers]
        if any(name.casefold() == other.casefold() for known in clashes for other in known):
            raise CommandError(f'Duplicate user file entry on line {number}.')
        target[name] = value
    if not admins or not cashiers:
        raise CommandError('User file needs an admins section and a PIN users section.')
    if len(register) > 1:
        raise CommandError('User file lists more than one register login.')
    if len(set(cashiers.values())) != len(cashiers):
        raise CommandError('PINs must be distinct.')
    return admins, register, cashiers


class Command(BaseCommand):
    help = 'Load admin logins, the shared register login and cashier PIN identities from a local file (counts only).'

    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--set-register-password', action='store_true',
                            help="Change an EXISTING register login's password to the file's (never done otherwise).")

    @sensitive_variables()
    def handle(self, *args, **options):
        admins, register, cashiers = parse_users(options['path'])
        User = get_user_model()
        listed = {name.casefold() for name in (*admins, *register, *cashiers)}
        with transaction.atomic():
            admin_group, register_group = group(ADMIN_GROUP), group(REGISTER_GROUP)
            for name, password in admins.items():
                user = User.objects.filter(username__iexact=name).first()
                # Do not silently promote or change an existing unrelated login (or the register login).
                if user and not user.groups.filter(pk=admin_group.pk).exists():
                    raise CommandError('An existing login conflicts with an admin in the user file.')
                if user is None:
                    user = User(username=name)
                if not user.check_password(password):
                    user.set_password(password)
                user.is_active = True
                user.save()
                user.groups.add(admin_group)
            register_note = 'no register login listed'
            for name, password in register.items():
                user = User.objects.filter(username__iexact=name).first()
                if user and user.groups.filter(pk=admin_group.pk).exists():
                    raise CommandError('The register login in the user file is an admin login.')
                if user is None:
                    user = User(username=name)
                    user.set_password(password)
                    register_note = 'register login created from the file'
                elif user.check_password(password):
                    register_note = 'register login password: same as file'
                elif options['set_register_password']:
                    user.set_password(password)
                    register_note = 'register login password: changed to the file'
                else:
                    register_note = ('register login password: differs from file (kept; '
                                     'use --set-register-password to change it)')
                user.is_active = True
                user.save()
                user.groups.add(register_group)
            for name, pin in cashiers.items():
                cashier = Cashier.objects.filter(name__iexact=name).first() or Cashier(name=name)
                if not check_password(pin, cashier.pin_hash):
                    cashier.pin_hash = make_password(pin)
                cashier.active = True
                cashier.save()
            # Not in the file any more: deactivate, never delete (history keeps the names).
            gone_admins = [u for u in User.objects.filter(is_active=True, groups=admin_group)
                           if u.username.casefold() not in listed]
            gone_registers = [u for u in User.objects.filter(is_active=True, groups=register_group)
                              if u.username.casefold() not in listed and u not in gone_admins]
            for user in gone_admins + gone_registers:
                user.is_active = False
                user.save(update_fields=['is_active'])
            gone_cashiers = [c for c in Cashier.objects.filter(active=True) if c.name.casefold() not in listed]
            for cashier in gone_cashiers:
                cashier.active = False
                cashier.save(update_fields=['active'])
        parts = [f'{len(admins)} admins', f'{len(register)} register login', f'{len(cashiers)} PIN users loaded']
        gone = [f'{n} {label}' for n, label in ((len(gone_admins), 'admin'), (len(gone_registers), 'register login'),
                                                (len(gone_cashiers), 'PIN users')) if n]
        line = ', '.join(parts) + ('; ' + ' and '.join(gone) + ' deactivated' if gone else '')
        self.stdout.write(f'{line}; {register_note}')
