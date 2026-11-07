# App users and cashier PINs

New installations create the first admin, optional register login and PIN cashiers through `/setup/` or `manage.py setup`. Admins manage logins and cashier PINs through **File → Users & Cashiers** (`/settings/users/`); see [the user guide](../docs/USER-GUIDE.md#users--cashiers). No terminal is needed for routine user maintenance.
Admin logins belong to the `Admin` group. The shared register login belongs to
the `register` group and uses `register` as its default username. Cashiers are
PIN identities, not Django logins. Admin Mode requires membership in `Admin`.

The optional file loader reads a local credential file:

```text
Admins
Demo Admin password <password>
Shared register User
register password <password>
Sub Accounts
Demo Cashier Pin <4 digits>
```

Run `python manage.py load_app_users <path> [--set-register-password]` with the
target installation's settings. Output contains counts, never credentials.
Reloading is case-insensitive and preserves unchanged password and PIN hashes.
The loader changes an existing register password only with
`--set-register-password`. Missing names are deactivated, preserving document
history. An admin may also have a cashier PIN identity; the register login may
not overlap either role. Duplicate names within a section and duplicate PINs
are rejected. The load is transactional.
