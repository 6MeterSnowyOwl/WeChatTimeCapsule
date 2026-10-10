from __future__ import annotations

import multiprocessing
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from wechat_exporter.capability_probe import (
    probe_account_database,
    probe_account_with_timeout,
)
from wechat_exporter.compatibility import (
    CapabilityProbeResult,
    ProbeErrorCode,
    RuntimeCapability,
)
from wechat_exporter.contacts import CONTACT_REQUIRED_COLUMNS
from wechat_exporter.messages import MESSAGE_REQUIRED_COLUMNS


def _slow_probe_child(send_connection, stable_id: str) -> None:
    del stable_id
    try:
        time.sleep(10)
    finally:
        send_connection.close()


def _ready_probe_child(send_connection, stable_id: str) -> None:
    del stable_id
    try:
        send_connection.send(
            CapabilityProbeResult(RuntimeCapability.READY).to_payload()
        )
    finally:
        send_connection.close()


class _ProbeDB:
    def __init__(self, contact_path: Path, message_path: Path):
        self._contact_path = contact_path
        self._message_path = message_path
        self._db_files = [("contact", str(contact_path), None)]

    def get_self_info(self):
        return {"nick_name": "Alice", "remark": "", "username": "private-account-id"}

    def _open(self, relative_path: str):
        path = self._contact_path if relative_path == "contact" else self._message_path
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def _message_dbs(self):
        return ["message"]


class CapabilityProbeTests(unittest.TestCase):
    def _database(self, path: Path, table: str, columns: tuple[str, ...]) -> None:
        connection = sqlite3.connect(path)
        try:
            definitions = ", ".join(f'"{column}" BLOB' for column in columns)
            connection.execute(f'CREATE TABLE "{table}" ({definitions})')
            connection.commit()
        finally:
            connection.close()

    def test_probe_returns_ephemeral_gui_display_name_and_stable_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact_path = root / "contact.db"
            message_path = root / "message.db"
            self._database(contact_path, "contact", CONTACT_REQUIRED_COLUMNS)
            self._database(message_path, "Msg_0123456789abcdef0123456789abcdef", MESSAGE_REQUIRED_COLUMNS)

            result = probe_account_database(_ProbeDB(contact_path, message_path))

            self.assertEqual(result.runtime_capability, RuntimeCapability.READY)
            self.assertEqual(result.error_code, ProbeErrorCode.NONE)
            self.assertEqual(result.detail, "")
            self.assertEqual(result.account_display_name, "Alice")
            self.assertEqual(
                CapabilityProbeResult.from_payload(result.to_payload()), result
            )
            self.assertNotIn("private-account-id", repr(result))

    def test_probe_fails_closed_when_contact_reader_schema_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact_path = root / "contact.db"
            message_path = root / "message.db"
            self._database(contact_path, "contact", CONTACT_REQUIRED_COLUMNS[:-1])
            self._database(message_path, "Msg_0123456789abcdef0123456789abcdef", MESSAGE_REQUIRED_COLUMNS)

            result = probe_account_database(_ProbeDB(contact_path, message_path))

            self.assertEqual(result.runtime_capability, RuntimeCapability.PROBE_FAILED)
            self.assertEqual(result.error_code, ProbeErrorCode.CONTACT_SCHEMA_UNSUPPORTED)

    def test_timeout_returns_stable_failure_and_reaps_spawned_child(self):
        before = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()

        result = probe_account_with_timeout(
            "opaque-account-id",
            timeout_seconds=0.1,
            child_target=_slow_probe_child,
        )

        elapsed = time.monotonic() - started
        after = {child.pid for child in multiprocessing.active_children()}
        self.assertLess(elapsed, 4.0)
        self.assertEqual(result.runtime_capability, RuntimeCapability.PROBE_FAILED)
        self.assertEqual(result.error_code, ProbeErrorCode.TIMEOUT)
        self.assertEqual(after - before, set())

    def test_source_mode_spawn_round_trip_returns_and_reaps_child(self):
        before = {child.pid for child in multiprocessing.active_children()}
        result = probe_account_with_timeout(
            "opaque-account-id",
            timeout_seconds=5.0,
            child_target=_ready_probe_child,
        )
        after = {child.pid for child in multiprocessing.active_children()}
        self.assertEqual(result.runtime_capability, RuntimeCapability.READY)
        self.assertEqual(result.error_code, ProbeErrorCode.NONE)
        self.assertEqual(after - before, set())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
