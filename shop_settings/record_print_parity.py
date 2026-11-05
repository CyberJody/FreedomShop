# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit recorder, separate from assertions. Run: python -m shop_settings.record_print_parity OUTPUT.json.

The historical parity_golden.json stays pinned to the pre-Business commit; do not replace it to accept a failure.
This uses only the isolated in-memory test database and never prints to hardware.
"""
import argparse
import json
import os
from pathlib import Path
import unittest

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    import django
    django.setup()
    from django.test.runner import DiscoverRunner
    from shop_settings.test_print_parity import GOLDEN, PrintParityTests
    if args.output.resolve() == GOLDEN.resolve():
        parser.error('Choose a separate output path; the historical golden is immutable.')

    class Recorder(PrintParityTests):
        def record(self):
            args.output.write_text(json.dumps(self.outputs(), indent=1, sort_keys=True) + '\n')

    runner = DiscoverRunner(verbosity=1)
    runner.setup_test_environment()
    old_config = runner.setup_databases()
    try:
        result = runner.run_suite(unittest.TestSuite([Recorder('record')]))
    finally:
        runner.teardown_databases(old_config)
        runner.teardown_test_environment()
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == '__main__':
    main()
