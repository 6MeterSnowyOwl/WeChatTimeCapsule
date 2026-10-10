"""Catches private upstream output and logs escaping ordinary account/probe calls."""
import io
from contextlib import closing
import json
import logging
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from wechat_exporter.contacts import CONTACT_REQUIRED_COLUMNS
from wechat_exporter.messages import MESSAGE_REQUIRED_COLUMNS


def run_synthetic_case(case):
    # This process and spawned probe use only the explicit synthetic package.
    from wechat_exporter.account import list_account_choices, create_database, create_database_for_account
    from wechat_exporter.capability_probe import probe_account_with_timeout
    progress = io.StringIO()
    handler = logging.StreamHandler(progress)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    if case == 'list':
        result = dict(account_count=len(list_account_choices()))
    elif case == 'database':
        database, backend = create_database_for_account('synthetic-stable')
        result = dict(account_valid=database.account == 'synthetic-locator', version=backend.package_version)
    elif case == 'legacy':
        displayed = []
        database, backend = create_database(print_func=displayed.append)
        result = dict(account_valid=database.account == 'synthetic-locator',
                      selection_displayed=any('synthetic-stable' in text for text in displayed))
    elif case == 'nested_failure':
        from wechat_exporter.account import quiet_upstream
        prior_streams = (sys.stdout, sys.stderr)
        try:
            with quiet_upstream():
                from wechatauto.db import emit_private_observation
                emit_private_observation()
                with quiet_upstream():
                    emit_private_observation()
                    raise KeyboardInterrupt('synthetic interruption')
        except KeyboardInterrupt:
            pass
        result = dict(streams_restored=(sys.stdout, sys.stderr) == prior_streams)
    else:
        # Reproduce the ordinary GUI parent's flag, which spawn does not inherit.
        from wechatauto.param import WxParam
        WxParam.ENABLE_FILE_LOGGER = False
        result_value = probe_account_with_timeout('synthetic-stable', timeout_seconds=8)
        result = dict(runtime=result_value.runtime_capability.value, code=result_value.error_code.value,
                      children_remaining=len(multiprocessing.active_children()))
    logging.getLogger().info('public-progress')
    result['public_logging_preserved'] = 'public-progress' in progress.getvalue()
    print(json.dumps(result))
    return 0


class UpstreamPrivacyTests(unittest.TestCase):
    def run_case(self, case, failure=''):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for filename, table, columns in (
                ('contact.db', 'contact', CONTACT_REQUIRED_COLUMNS),
                ('message.db', 'Msg_0123456789abcdef0123456789abcdef', MESSAGE_REQUIRED_COLUMNS),
            ):
                with closing(sqlite3.connect(root / filename)) as connection:
                    definitions = ','.join(f'"{name}" BLOB' for name in columns)
                    connection.execute(f'CREATE TABLE "{table}" ({definitions})')
            source_root = Path(__file__).resolve().parents[1]
            environment = dict(os.environ)
            marker = 'synthetic-private-' + uuid4().hex
            environment.update(SYNTHETIC_PRIVATE_MARKER=marker, SYNTHETIC_DB_ROOT=str(root),
                SYNTHETIC_UPSTREAM_FAILURE=failure,
                PYTHONPATH=os.pathsep.join((str(source_root / 'tests/fixtures/privacy_upstream'), str(source_root))))
            completed = subprocess.run([sys.executable, '-m', 'tests.test_upstream_privacy', '--synthetic-case', case],
                cwd=root, env=environment, text=True, capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 0, 'synthetic child unexpectedly failed')
            leaked_output = marker in completed.stdout or marker in completed.stderr
            log_paths = list((root / 'wechatauto_logs').glob('*'))
            leaked_files = any(marker.encode() in path.read_bytes() for path in log_paths if path.is_file())
            # Aggregate-only assertions never put the marker or private output in test failures.
            self.assertFalse(leaked_output, 'private upstream output escaped')
            self.assertFalse(leaked_files, 'private upstream file log escaped')
            self.assertFalse(log_paths, 'upstream created a default log file')
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            return result

    def test_account_listing_silences_private_output_and_retains_public_logging(self):
        result = self.run_case('list')
        self.assertEqual(result['account_count'], 1)
        self.assertTrue(result['public_logging_preserved'])

    def test_account_database_initialization_is_silent_and_usable(self):
        result = self.run_case('database')
        self.assertTrue(result['account_valid'])
        self.assertEqual(result['version'], '1.2.1')
        self.assertTrue(result['public_logging_preserved'])

    def test_legacy_account_selection_still_displays_its_intended_identity(self):
        result = self.run_case('legacy')
        self.assertTrue(result['account_valid'])
        self.assertTrue(result['selection_displayed'])
        self.assertTrue(result['public_logging_preserved'])

    def test_real_spawn_probe_is_silent_and_ready(self):
        result = self.run_case('probe')
        self.assertEqual((result['runtime'], result['code']), ('READY', 'NONE'))
        self.assertEqual(result['children_remaining'], 0)

    def test_real_spawn_constructor_failure_is_silent_and_fails_closed(self):
        result = self.run_case('probe', 'constructor')
        self.assertEqual((result['runtime'], result['code']), ('PROBE_FAILED', 'DATABASE_OPEN_FAILED'))
        self.assertEqual(result['children_remaining'], 0)

    def test_real_spawn_query_failure_is_silent_and_fails_closed(self):
        result = self.run_case('probe', 'query')
        self.assertEqual((result['runtime'], result['code']), ('PROBE_FAILED', 'CONTACT_QUERY_FAILED'))
        self.assertEqual(result['children_remaining'], 0)

    def test_nested_base_exception_restores_streams_and_application_logging(self):
        result = self.run_case('nested_failure')
        self.assertTrue(result['streams_restored'])
        self.assertTrue(result['public_logging_preserved'])


if __name__ == '__main__':
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == '--synthetic-case':
        raise SystemExit(run_synthetic_case(sys.argv[2]))
    unittest.main()
